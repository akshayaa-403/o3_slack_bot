import { DynamoDBClient } from "@aws-sdk/client-dynamodb";
import {
  DynamoDBDocumentClient,
  UpdateCommand,
  GetCommand,
  DeleteCommand
} from "@aws-sdk/lib-dynamodb";
import { BedrockRuntimeClient, InvokeModelCommand } from "@aws-sdk/client-bedrock-runtime";
// Or if not using shared layer
const BUCKET_RESULT_NAME = process.env.REPORT_BUCKET;
const docClient = DynamoDBDocumentClient.from(new DynamoDBClient({}));
const table = process.env.DYNAMO_TABLE || "03_v2_tickets";
const bedrockClient = new BedrockRuntimeClient({ region: "us-east-1" });
const CLAUDE_BLACK_BOX_ID = process.env.CLAUDE_USER_ID;

export const handler = async (event) => {
  console.log("Received Event:", JSON.stringify(event));
  const channelId = event.channelId;
  const threadTs = event.threadTs;
  
  const JIRA_WEBHOOK = process.env.JIRA_WEBHOOK;
  const JIRA_AUTOMATION_WEBHOOK_TOKEN = process.env.JIRA_WEBHOOK_TOKEN;
  
  // Fetch session from DB
  const { userId, slackData, originalThreadId, originalChannelId } = await getPMSession(channelId, threadTs);
  // Fetch ONLY messages after MPSR start
  // Filter out Bot messages
  let allMessages = [];
  let cursor = null;
  
  try {
    do {
      const res = await fetch(
        `https://slack.com/api/conversations.history?channel=${channelId}&thread_ts=${threadTs}&inclusive=true${cursor ? `&cursor=${cursor}` : ""}`,
        {
          headers: { Authorization: `Bearer ${process.env.SLACK_BOT_TOKEN}` }
        }
      );
      console.log("fetching Slack comms....");
      const data = await res.json();
      
      if (!data.ok) {
        throw new Error(`Slack history error: ${data.error}`);
      }
      
      const messages = data.messages || [];
      allMessages = allMessages.concat(messages);
      cursor = data.response_metadata?.next_cursor;
    } while (cursor);
    
    const filteredMessages = allMessages.filter(
      m => m.user !== CLAUDE_BLACK_BOX_ID
    );
    
    if (filteredMessages.length === 0) {
      console.log("[MPSR] No original threads in MPSR session - skipping MPSR sentiment");
    } else {
      // IF SENTIMENT TRACKING -> only the requester's messages, attributed to ORIGINAL support thread
      if (process.env.SENTIMENT_TRACKING === "true") {
        try {
          const { sentiment, sentimentScore } = await detectSentiment(allMessages[0]?.text);
          await trackSentiment({
            source: "MPSR",
            channelId: originalChannelId,
            threadTs: originalThreadId,
            sentiment: sentiment,
            sentimentScore: sentimentScore,
            userMail: slackData.user?.profile?.email || null,
            dynamoTable: process.env.DYNAMO_TABLE_STATS
          });
        } catch (e) {
          console.error("[SENTIMENT] MPSR tracking failed (non-fatal):", e);
        }
      }
    }
    
    // 2. Clean history
    const cleanHistory = filteredMessages
      /*.filter(m => 
        (m.text !== "test") && 
        ...
      )*/
      .map(m => {
        let text = m.text || "";
        if (text.includes("You selected")) {
          text = "[Actions with delete the entire conversation and summary]";
        }
        if (text.includes("This action will delete the entire conversation and summary")) {
          text = "[Actions with delete the entire conversation and closing the MPSR]";
        }
        return {
          user: m.user === slackData.user?.id ? "User" : "Agent",
          text: text
        };
      })
      .filter(m => m.text.trim().length > 0)
      .reverse();
      
    console.log("App history : " + cleanHistory);
    if (cleanHistory.length === 0) {
      console.log("No clean history found - stopping processing!");
      return;
    }
    
    // 3. Resolve Atlassian Account ID
    const { accountId: atlassianAccountId, agentAccountId } = await resolveAtlassianAccount(
      userId,
      slackData?.user?.profile?.email
    );
    
    console.log("Resolved IDs: ", { userAccountId: atlassianAccountId, agentAccountId });
    
    if (!atlassianAccountId && !agentAccountId) {
      console.error("Failed to resolve user or agent Atlassian ID - aborting");
      throw new Error("Atlassian ID resolution failed");
    }
    
    console.log(`payload : cleanHistory = ${cleanHistory} - atlassianAccountId = ${atlassianAccountId} - agentAccountId = ${agentAccountId} - userId = ${userId} - channel = ${channelId}`);
    let webhookSuccess = false;
    
    // 4. Log to S3
    const archivePayload = {
      userId,
      channelId,
      threadTs,
      timestamp: new Date().toISOString(),
      maxMessages: process.env.MAX_MESSAGES_LOG_LEN,
      messages: cleanHistory,
      cleanConversation: cleanHistory
    };
    
    const s3Key = `mpsr/exports/${channelId}-${dateToFileName(new Date())}.txt`;
    await s3.putObject({
      Bucket: BUCKET_RESULT_NAME,
      Key: s3Key,
      Body: JSON.stringify(archivePayload),
      ContentType: "text/plain"
    }).promise();
    
    console.log("Archived to S3: ", s3Key);
    
    // 5. Send to Jira ticket summary
    const summaryText = await summarizeConversation(cleanHistory);
    const summary = summaryText;
    
    const JIRA_CHAR_LIMIT = 32760;
    const SUMMARY_LIMIT = 250;
    
    let summary2 = summary;
    if (summary.length > JIRA_CHAR_LIMIT) {
      summary2 = summary.substring(0, JIRA_CHAR_LIMIT - 3) + "...";
    }
    
    console.log("Final Summary: ", summary2);
    
    // 6. Send to Jira
    const response = await fetch(JIRA_WEBHOOK, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Automation-Webhook-Token": JIRA_AUTOMATION_WEBHOOK_TOKEN
      },
      body: JSON.stringify({
        conversation: summary2,
        user: atlassianAccountId,
        agent: agentAccountId,
        userSlackId: userId,
        channelId: channelId
      })
    });
    
    console.log("Jira status:", response.status);
    console.log("Jira response:", await response.text());
    
    if (response.status >= 200 && response.status < 300) {
      webhookSuccess = true;
    } else {
      throw new Error(`Jira Webhook failed. Status: ${response.status}`);
    }
    
    webhookSuccess = true;
    
    // 6a. Notify Slack (only if webhook succeeded)
    await fetch("https://slack.com/api/chat.postMessage", {
      method: "POST",
      headers: {
        Authorization: `Bearer ${process.env.SLACK_BOT_TOKEN}`,
        "Content-Type": "application/json"
      },
      body: JSON.stringify({
        channel: channelId,
        text: "Conversation exported. Cleaning up..."
      })
    });
    
    // 7 & 8. Delete ONLY if Webhook success is confirmed
    if (webhookSuccess) {
      console.log("Webhook succeeded - starting cleanup");
      
      // Delete messages + thread replies
      const messagesToReply = filteredMessages;
      for (const msg of messagesToReply) {
        if (msg.ts === threadTs) continue;
        
        if (msg.reply_count && msg.reply_count > 0) {
          console.log(`Fetching thread replies for ${msg.ts}...`);
          
          const threadReplies = await fetch(
            `https://slack.com/api/conversations.replies?channel=${channelId}&ts=${msg.ts}`,
            {
              headers: { Authorization: `Bearer ${process.env.SLACK_BOT_TOKEN}` }
            }
          );
          
          const repliesData = await threadReplies.json();
          if (repliesData.ok) {
            for (const reply of repliesData.messages) {
              if (reply.ts === threadTs) continue;
              
              // Skip messages BEFORE MPSR was created
              if (Number(reply.ts) < Number(event.mpsrCreatedTs)) {
                console.log("Skipping old message", reply.ts);
                continue;
              }
              
              console.log("Deleting thread reply:", reply.ts);
              await fetch("https://slack.com/api/chat.delete", {
                method: "POST",
                headers: {
                  Authorization: `Bearer ${process.env.SLACK_USER_TOKEN}`,
                  "Content-Type": "application/json"
                },
                body: JSON.stringify({
                  channel: channelId,
                  ts: reply.ts
                })
              });
            }
          }
        }
        
        // 2. Delete parent message
        console.log("Deleting parent message:", msg.ts);
        await fetch("https://slack.com/api/chat.delete", {
          method: "POST",
          headers: {
            Authorization: `Bearer ${process.env.SLACK_USER_TOKEN}`,
            "Content-Type": "application/json"
          },
          body: JSON.stringify({
            channel: channelId,
            ts: msg.ts
          })
        });
      }
      
      await new Promise(resolve => setTimeout(resolve, 250));
      
      console.log("Deleting MPSR session...");
      
      // Delete DB record
      await docClient.send(new DeleteCommand({
        TableName: table,
        Key: { channelId }
      }));
      
      console.log("Cleanup complete.");
    } else {
      console.log("Skipping cleanup - webhook not successful");
    }
  } catch (err) {
    console.error("ERROR:", err);
    await fetch("https://slack.com/api/chat.postMessage", {
      method: "POST",
      headers: {
        Authorization: `Bearer ${process.env.SLACK_BOT_TOKEN}`,
        "Content-Type": "application/json"
      },
      body: JSON.stringify({
        channel: channelId,
        text: "Error processing conversation."
      })
    });
  }
};

/*******************************************************
 *                     HELPERS                          *
 *******************************************************/

async function getPMSession(channelId) {
  const result = await docClient.send(new GetCommand({
    TableName: table,
    Key: { channelId }
  }));
  
  if (!result.Item) {
    throw new Error("MPSR session not found");
  }
  
  console.log("MPSR session data:", JSON.stringify(result.Item));
  
  return {
    userId: result.Item.userId,
    agentId: result.Item.agentId,
    originalThreadId: result.Item.originalThreadId || null,
    originalChannelId: result.Item.originalChannelId || null
  };
}

async function resolveAtlassianAccount(slackUserId) {
  console.log("Resolving Slack user:", slackUserId);
  
  const slackRes = await fetch(
    `https://slack.com/api/users.info?user=${slackUserId}`,
    {
      headers: { Authorization: `Bearer ${process.env.SLACK_BOT_TOKEN}` }
    }
  );
  
  console.log("Slack response received");
  const slackData = await slackRes.json();
  
  if (!slackData.ok) {
    console.log("Slack user info failed:", slackData.error);
    return null;
  }
  
  const email = slackData.user?.profile?.email;
  if (!email) return null;
  
  const auth = Buffer.from(
    `${process.env.ATLASSIAN_EMAIL}:${process.env.ATLASSIAN_API_TOKEN}`
  ).toString("base64");
  
  const res = await fetch(
    `${process.env.ATLASSIAN_DOMAIN}/rest/api/3/user/search?query=${encodeURIComponent(email)}`,
    {
      headers: {
        Authorization: `Basic ${auth}`,
        Accept: "application/json"
      }
    }
  );
  
  const users = await res.json();
  if (users && users.length > 0) {
    return users[0].accountId;
  }
  return null;
}

async function summarizeConversation(history) {
  const prompt = `
Summarize the following Slack conversation in 300 characters or less for a support ticket description. Include: Issue (what the user asked initially), Key details, Troubleshooting already done and the resolution provided.

Conversation:
${JSON.stringify(history)}
`;

  const command = new InvokeModelCommand({
    modelId: process.env.AN_CLAUDE_PROFILE_ARN,
    contentType: "application/json",
    accept: "application/json",
    body: JSON.stringify({
      messages: [
        {
          role: "user",
          content: [{ type: "text", text: prompt }]
        }
      ],
      anthropic_version: "bedrock-2023-05-31",
      max_tokens: 250,
      temperature: 0.3
    })
  });
  
  const response = await bedrockClient.send(command);
  const decoded = JSON.parse(
    new TextDecoder().decode(response.body)
  );
  
  console.log("MPSR Full RESPONSE:", JSON.stringify(decoded, null, 2));
  
  const summary = decoded.content?.[0]?.text || "";
  if (!summary) {
    console.log("No summary generated.");
  }
  
  console.log("MPSR Summary:", summary);
  return summary;
}

function dateToFileName(d) {
  return `${d.getFullYear()}-${d.getMonth() + 1}-${d.getDate()}_${d.getHours()}-${d.getMinutes()}`;
}

