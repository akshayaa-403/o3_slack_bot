import { DynamoDBClient } from "@aws-sdk/client-dynamodb";
import { DynamoDBDocumentClient, UpdateCommand } from "@aws-sdk/lib-dynamodb";
import { SchedulerClient, DeleteScheduleCommand } from "@aws-sdk/client-scheduler";
import { S3Client, PutObjectCommand } from "@aws-sdk/client-s3";

const client = new DynamoDBClient({ region: "us-west-2" });
const ddbDocClient = DynamoDBDocumentClient.from(client);
const schedulerClient = new SchedulerClient({ region: "us-west-2" });
const s3Client = new S3Client({ region: "us-west-2" });

function normalizeEvent(event) {
  // ----------------------------------------------------
  // CASE 1: Existing Slack/EventBridge payload
  // ----------------------------------------------------
  if (event.channelId && event.threadTs) {
    return {
      channelId: event.channelId,
      threadTs: event.threadTs,
      userId: event.userId,
      source: "SLACK"
    };
  }

  // ----------------------------------------------------
  // CASE 2: Amazon Lex payload
  // ----------------------------------------------------
  const attrs = event.sessionState?.sessionAttributes;
  if (attrs?.slackChannelId && attrs?.slackThreadTs) {
    return {
      channelId: attrs.slackChannelId,
      threadTs: attrs.slackThreadTs,
      userId: attrs.slackUserId,
      source: "LEX"
    };
  }

  throw new Error("Unsupported event payload format!");
}

export const handler = async (event) => {
  const {
    channelId,
    threadTs,
    userId,
    source
  } = normalizeEvent(event);

  console.log("Normalized source:", source);
  console.log(
    "Summarizer ring Event",
    JSON.stringify(event, null, 2)
  );

  const POWER_AUTOMATE_URL = process.env.POWER_AUTOMATE_URL;

  // 1. Get history from Slack
  const slackRes = await fetch(`https://slack.com/api/conversations.replies?channel=${channelId}&ts=${threadTs}`, {
    headers: { Authorization: `Bearer ${process.env.SLACK_BOT_TOKEN}` }
  });
  const history = await slackRes.json();
  if (!history.ok) {
    throw new Error(`Slack API failed: ${history.error}`);
  }

  // 2. Filter the history
  const cleanHistory = (history.messages || [])
    .filter(m => {
      const text = m.text || "";

      // exclude junk messages
      return (
        !text.includes("I haven't heard from you") &&
        !text.includes("Summarizing conversation and closing thread") &&
        !text.includes("No response received")
      );
    })
    .map(m => {
      const isBot = (
        m.bot_id ||
        m.subtype === "bot_message" ||
        !m.app_id
      );
      const sender = isBot ? "Bot" : "User";
      let messageText = m.text || "";

      // ALWAYS extract text from Slack blocks
      let blockText = "";

      if (Array.isArray(m.blocks)) {
        const blockTexts = [];

        for (const block of m.blocks) {
          // section block
          if (block.type === "section") {
            if (block.text?.text) {
              blockTexts.push(block.text.text);
            }
            // fields[]
            if (Array.isArray(block.fields)) {
              for (const field of block.fields) {
                if (field.text) {
                  blockTexts.push(field.text);
                }
              }
            }
          }
          // context block
          if (block.type === "context" && Array.isArray(block.elements)) {
             for (const el of block.elements) {
               if (el.text) {
                 blockTexts.push(el.text);
               }
             }
          }
          // rich_text block support
          if (block.type === "rich_text" && Array.isArray(block.elements)) {
            const extractRichText = elements => {
              let text = "";
              for (const el of elements) {
                if (el.type === "text") {
                  text += el.text;
                }
                if (Array.isArray(el.elements)) {
                  text += extractRichText(el.elements);
                }
              }
              return text;
            };
            blockTexts.push(extractRichText(block.elements));
          }
        }
        blockText = blockTexts
          .filter(Boolean)
          .join("\n")
          .trim();
      }

      // Merge text + blocks intelligently

      const placeholderTexts = [
        "Response",
        "Processing your request"
      ];
      
      // Prefer block text if normal text is useless
      if (placeholderTexts.includes(messageText.trim())) {
        messageText = blockText || messageText;
      }
      // Otherwise append block content if different
      else if (
        blockText &&
        !messageText.includes(blockText)
      ) {
         messageText += `\n${blockText}`;
      }

      // Attachments fallback
      if (!messageText && m.attachments) {
        if (Array.isArray(m.attachments)) {
          messageText = m.attachments
            .map(a => a.fallback || "")
            .join("\n");
        }
      }

      return `${sender}: ${messageText.trim()}`;
    });

  console.log("Clean History for Jira:", cleanHistory);

  let userEmail = null;
  if (cleanHistory.length > 0) {
    // -- CHECK ACCOUNT --
    const slackUserRes = await fetch(`https://slack.com/api/users.info?user=${userId}`, {
      headers: { Authorization: `Bearer ${process.env.SLACK_BOT_TOKEN}` }
    });
    const slackUserData = await slackUserRes.json();
    if (slackUserData.ok) {
      userEmail = slackUserData.user?.profile?.email || null;
    }
  }

  // 3. Sending to power automate log...
  console.log("Sending data to Power Automate...");
  console.log("POWER_AUTOMATE_URL = ", POWER_AUTOMATE_URL);

  if (!POWER_AUTOMATE_URL) {
    throw new Error("POWER_AUTOMATE_URL env var is missing");
  }

  const payload = {
    type: "log",
    conversation: cleanHistory,
    rawSlackHistory: history,
    userEmail: userEmail,
    channelId,
    threadTs
  };
  console.log("Payload:", JSON.stringify(payload));
  const postRes = await fetch(POWER_AUTOMATE_URL, {
    method: "POST",
    headers: {
      "Content-Type": "application/json"
    },
    body: JSON.stringify(payload)
  });

  if (!postRes.ok) {
    const errText = await postRes.text();
    throw new Error(`Power Automate failed: ${postRes.status} - ${errText}`);
  }

  // 4. Notify the user in Slack
  await fetch("https://slack.com/api/chat.postMessage", {
    method: "POST",
    headers: {
      Authorization: `Bearer ${process.env.SLACK_BOT_TOKEN}`,
      "Content-Type": "application/json"
    },
    body: JSON.stringify({
      channel: channelId,
      thread_ts: threadTs,
      text: `The session for <@${userId}> has been closed.`
    })
  });
  
  // 4a. Send the feedback prompt as a follow-up message
  await sendFeedbackPrompt(channelId, threadTs);

  // STORE RAW AUDIT LOG IN S3
  try {
    const s3DateKey = [
      "slack-audit",
      new Date().toISOString().split("T")[0], // yyyy-mm-dd
      `${threadTs}.json`
    ].join("/");

    await s3Client.send(new PutObjectCommand({
      Bucket: process.env.AUDIT_S3_BUCKET,
      Key: s3DateKey,
      Body: JSON.stringify({
        timestamp: new Date().toISOString(),
        userId,
        userEmail,
        channelId,
        threadTs,
        cleanConversation: cleanHistory,
        rawSlackHistory: history
      }, null, 2),
      ContentType: "application/json"
    }));

    console.log(`Audit log uploaded to S3: ${s3DateKey}`);
  } catch (err) {
    console.error("SUMMARIZER ERROR during processing:", err);

    await fetch("https://slack.com/api/chat.postMessage", {
      method: "POST",
      headers: {
        Authorization: `Bearer ${process.env.SLACK_BOT_TOKEN}`,
        "Content-Type": "application/json"
      },
      body: JSON.stringify({
        channel: channelId,
        thread_ts: threadTs,
        text: "There was an issue processing the session, but I'm closing this thread as requested."
      })
    });
  } finally {
    console.log(`Cleaning up session: ${threadTs}`);
    try {
      // 5. DynamoDB Update
      await ddbDocClient.send(new UpdateCommand({
        TableName: process.env.DYNAMO_TABLE,
        Key: { threadTs },
        UpdateExpression: "SET isClosed = :t, #st = :s",
        ExpressionAttributeNames: { "#st": "state" },
        ExpressionAttributeValues: { ":t": true, ":s": "CLOSED" }
      }));

      // 6. EventBridge Scheduler Delete
      if (threadTs) {
        const safeTs = threadTs.replace(/[^a-zA-Z0-9-_]/g, "_");
        await schedulerClient.send(new DeleteScheduleCommand({
          Name: `slack-timeout-${safeTs}`
        }));
      }
    } catch (cleanupErr) {
      if (cleanupErr.name === 'ResourceNotFoundException') {
        console.log("Schedule already deleted or not found.");
      } else {
        console.error("CRITICAL ERROR during cleanup:", cleanupErr);
      }
    }
  }
};

async function sendFeedbackPrompt(channelId, threadTs) {
  try {
    await fetch("https://slack.com/api/chat.postMessage", {
      method: "POST",
      headers: {
        Authorization: `Bearer ${process.env.SLACK_BOT_TOKEN}`,
        "Content-Type": "application/json"
      },
      body: JSON.stringify({
        channel: channelId,
        thread_ts: threadTs,
        blocks: [
          {
            type: "section",
            text: {
              type: "mrkdwn",
              text: "How was your experience with Ozono Bot today?\nTap a star to rate!"
            }
          },
          {
            type: "actions",
            block_id: "feedback_stars",
            elements: [
              { type: "button", text: { type: "plain_text", text: "⭐", emoji: true }, action_id: "feedback_1", value: "1" },
              { type: "button", text: { type: "plain_text", text: "⭐⭐", emoji: true }, action_id: "feedback_2", value: "2" },
              { type: "button", text: { type: "plain_text", text: "⭐⭐⭐", emoji: true }, action_id: "feedback_3", value: "3" },
              { type: "button", text: { type: "plain_text", text: "⭐⭐⭐⭐", emoji: true }, action_id: "feedback_4", value: "4" },
              { type: "button", text: { type: "plain_text", text: "⭐⭐⭐⭐⭐", emoji: true }, action_id: "feedback_5", value: "5" }
            ]
          }
        ]
      })
    });
    console.log(`Feedback prompt sent to thread: ${threadTs}`);
  } catch (err) {
    console.error("Failed to send feedback prompt", err);
  }
}

