const { DynamoDBClient } = require("@aws-sdk/client-dynamodb");
const { DynamoDBDocumentClient, UpdateCommand } = require("@aws-sdk/lib-dynamodb");
const { EventBridgeClient, DeleteRuleCommand } = require("@aws-sdk/client-eventbridge");

const db = DynamoDBDocumentClient.from(new DynamoDBClient({}));
const scheduler = new EventBridgeClient({});

export const handler = async (event) => {
    const { channelID, threadID, userID } = event;
    const { SLACK_BOT_TOKEN, JIRA_WEBHOOK_URL, ATLASSIAN_EMAIL, ATLASSIAN_API_TOKEN, DYNAMODB_TABLE } = process.env;

    try {
        // 1. Get History from Slack
        const slackRes = await fetch(`https://slack.com/api/conversations.replies?channel=${channelID}&ts=${threadID}`, {
            headers: { 'Authorization': `Bearer ${SLACK_BOT_TOKEN}` }
        });
        const history = await slackRes.json();

        // 2. Filter the history
        const cleanHistory = history.messages.filter(m => {
            const text = m.text || "";
            return !m.bot_id && !text.includes("bot_message");
        });

        // 3. Send to Jira Webhook
        const res = await fetch(JIRA_WEBHOOK_URL, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-Atlassian-Webhook-Token': 'jirashooktoken' },
            body: JSON.stringify({
                "conversation": cleanHistory,
                "channelID": channelID,
                "threadID": threadID
            })
        });

        // 4. Notify the user in Slack
        await fetch('https://slack.com/api/chat.postMessage', {
            method: 'POST',
            headers: { 'Authorization': `Bearer ${SLACK_BOT_TOKEN}`, 'Content-Type': 'application/json' },
            body: JSON.stringify({
                channel: channelID,
                thread_ts: threadID,
                text: "The session for this issue is now closed."
            })
        });

    } catch (err) {
        console.error("SUMMARIZER ERROR during processing", err);
        // ... Error notification logic
    } finally {
        // 5. DynamoDB Update
        await db.send(new UpdateCommand({
            TableName: DYNAMODB_TABLE,
            Key: { threadID },
            UpdateExpression: "SET #state = :state",
            ExpressionAttributeNames: { "#state": "state" },
            ExpressionAttributeValues: { ":state": "CLOSED" }
        }));
        
        // 6. EventBridge Scheduler Delete
        try {
            await scheduler.send(new DeleteRuleCommand({ Name: `slack-timeout-${threadID}` }));
        } catch (e) {
            console.log("Schedule already deleted or not found.");
        }
    }
};

