import { DynamoDBClient } from "@aws-sdk/client-dynamodb";
import { DynamoDBDocumentClient, PutCommand } from "@aws-sdk/lib-dynamodb";

const ddb = new DynamoDBClient({});
const docClient = DynamoDBDocumentClient.from(ddb);

export const handler = async (event) => {
    
    const CLOSE_ID_A = process.env.CLOSE_ID_A || '1e8df0f2-7661-4de2-bf53-06ac9c7bd3de';
    const SCHEDULE_ID_A = process.env.SCHEDULE_ID_A || '6eb8dbdf-68d7-4da1-a75d-bfbe28c05fad';
    const SLACK_CHANNEL = process.env.SLACK_CHANNEL || 'C07N3E9EFTU';
    const INSTANCE_ID = process.env.INSTANCE_ID || '1';
    const KOTLIN_TOKEN = process.env.KOTLIN_TOKEN;
    const TABLE_NAME = process.env.TABLE_NAME;

    const authHeader = `Basic ${Buffer.from(`${process.env.JIRA_API_USER}:${process.env.JIRA_API_TOKEN}`).toString('base64')}`;

    const commonHeaders = {
        'Authorization': authHeader,
        'Accept': 'application/json'
    };

    try {
        // --- Step 1: Fetching current on-call users ---
        console.log('Step 1: Fetching current on-call users...');
        const oncallUrl = `https://atlassian.net/rest/api/3/user/search?query=oncall`;
        const oncallResponse = await fetch(oncallUrl, { headers: commonHeaders });

        if (!oncallResponse.ok) {
            const errorText = await oncallResponse.text();
            throw new Error(`Jira API error: On-Call error API error: ${oncallResponse.status}`);
        }

        const participants = await oncallResponse.json();
        console.log(`Step 1 participants: ${participants}`);

        if (!participants || participants.length === 0) {
            return {
                statusCode: 404,
                body: JSON.stringify({ message: "No active on-call users found" })
            };
        }

        // --- Step 2: Fetching details for all users ---
        console.log(`Found ${participants.length} participants. Fetching details...`);
        
        const userDetailsPromises = participants.map(async (participant) => {
            const userUrl = participant.self;
            const accountId = participant.accountId;
            
            const userDetailsResponse = await fetch(userUrl, { headers: commonHeaders });
            
            if (!userDetailsResponse.ok) {
                return null;
            }

            const userResponse = await userDetailsResponse.json();
            
            return {
                accountId: accountId,
                displayName: userResponse.displayName,
                emailAddress: userResponse.emailAddress || ""
            };
        });

        const userDetailsResults = await Promise.all(userDetailsPromises);
        const users = userDetailsResults.filter(u => u !== null);

        console.log(`Successfully retrieved all users: ${users}`);
        console.log(`Syncing users to DynamoDB...`);

        // DynamoDB TTL Unix timestamp seconds
        const expirationTime = Math.floor(Date.now() / 1000) + (180 * 24 * 60 * 60);

        // --- Step 3: Saving to DynamoDB ---
        const ddbPromises = users.map(async (user) => {
            if (!user.emailAddress) {
                // Skipping lock or empty emails
                return null;
            }

            return docClient.send(new PutCommand({
                TableName: TABLE_NAME,
                Item: {
                    email: user.emailAddress,       // Partition Key (easier for Slack Lookup)
                    accountId: user.accountId,
                    displayName: user.displayName,
                    instanceId: INSTANCE_ID,
                    ttl: expirationTime
                }
            }));
        });

        const ddbResults = await Promise.all(ddbPromises);
        const successCount = ddbResults.filter(r => r !== null).length;
        
        console.log(`Successfully synced all users to DynamoDB.`);

        return {
            statusCode: 200,
            body: JSON.stringify({
                message: "On-call users synced successfully",
                count: successCount,
                users: users
            })
        };

    } catch (error) {
        console.error('Lambda execution failed:', error);
        return {
            statusCode: 500,
            body: JSON.stringify({ error: error.message })
        };
    }
};


