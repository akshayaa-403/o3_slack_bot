import { LambdaClient, InvokeCommand } from "@aws-sdk/client-lambda";
import { DynamoDBClient } from "@aws-sdk/client-dynamodb";
import { DynamoDBDocumentClient, UpdateCommand, GetCommand } from "@aws-sdk/lib-dynamodb";
import { SchedulerClient, DeleteScheduleCommand } from "@aws-sdk/client-scheduler";

const lambdaClient = new LambdaClient({});
const ddb = DynamoDBDocumentClient.from(new DynamoDBClient({}));

const CONFIG_TABLE = process.env.CONFIG_TABLE;
const schedulerClient = new SchedulerClient({});

/* ==========================================================
      DIRECT LAMBDA ROUTES
========================================================== */
const direct_lambda_routes = {
    '01_jira_summarizer': process.env.URL_01_JIRA_SUMMARIZER,
    '02_jira_troubleshooter': process.env.URL_02_JIRA_TROUBLESHOOTER
}

/* ==========================================================
      MAIN HANDLER
========================================================== */
export const handler = async (event) => {
    console.log(
        "LEX EVENT:",
        JSON.stringify(event, null, 2)
    );

    const intentName = event?.sessionState?.intent?.name;

    if (!intentName) {
        return failResponse(
            event,
            "Intent name missing."
        );
    }

    try {
        /* ==========================================================
              AUTOMATION ACTION ROUTING
        ========================================================== */
        const inputTranscript = event?.inputTranscript || "";

        if (inputTranscript === "ignore") {
            await invokeReturn(
                process.env.URL_01_JIRA_SUMMARIZER,
                event
            );
            await closeSlackSession(event);
            return successResponse(
                event,
                "Great, let me know if you need help with anything else."
            );
        }

        /* ==========================================================
              CREATE JIRA TICKET DIRECTLY
        ========================================================== */
        if (intentName === "CreateJiraTicket") {
            const invocationSource = event.invocationSource;
            const slots = event.sessionState.intent.slots || {};
            const description = getSlotValue(slots, "jira_description");

            // DIALOG PHASE
            if (invocationSource === "DialogCodeHook") {
                if (!description) {
                    return {
                        sessionState: {
                            ...event.sessionState,
                            dialogAction: {
                                slotToElicit: "jira_description",
                                type: "ElicitSlot"
                            },
                            intent: {
                                ...event.sessionState.intent,
                                state: "InProgress"
                            }
                        },
                        messages: [
                            {
                                contentType: "PlainText",
                                content: "Sure - please describe the issue briefly."
                            }
                        ]
                    };
                }

                return {
                    sessionState: {
                        ...event.sessionState,
                        dialogAction: {
                            type: "Delegate"
                        }
                    }
                };
            }

            // FULFILLMENT PHASE
            if (invocationSource === "FulfillmentCodeHook") {
                const payload = {
                    config: await getConfig(),
                    event: event.sessionState.sessionAttributes || {},
                    description,
                    slack: {
                        threadTs: slots.slackChannelId,
                        channelId: slots.slackChannelId
                    },
                    source: "slack",
                    user: slots.slackUserId
                };

                console.log(
                    "Creating Jira Ticket",
                    JSON.stringify(payload, null, 2)
                );

                await closeSlackSession(event);

                const webhookToken = process.env.CREATE_TICKET_TOKEN;
                await invokeWebhook(
                    payload,
                    webhookToken
                );

                return successResponse(
                    event,
                    "Your request has been submitted successfully."
                );
            }
        }

        /* ==========================================================
              DIRECT LAMBDA ROUTING
        ========================================================== */
        if (direct_lambda_routes[intentName]) {
            await invokeReturn(
                direct_lambda_routes[intentName],
                event
            );
        }

        /* ==========================================================
              CONFIG LOOKUP WITH ORIGINAL INTENT
        ========================================================== */
        // Read originalIntent from session attributes
        // This was saved by prod_slack_node_lambda_router
        // Lex first uploads the user's question
        // then runs access_normal_portal workflow
        const originalIntent = event.sessionState.sessionAttributes?.originalIntent || "";

        let config = null;

        // Step 1 - try originalIntent first (e.g. Home = generalAccess -> branching: 'config-branching')
        if (originalIntent) {
            config = await getConfig(originalIntent);
            console.log("Original intent config text found for:", originalIntent);
        }

        // Step 2 - try current intent if not found (e.g. Home = generalAccess -> branching: 'config-branching')
        if (!config) {
            config = await getConfig(intentName);
            console.log("Using current intent config text found for:", intentName);
        }

        // Step 3 - Final fallback to 'autoCreateTicket'
        if (!config) {
            config = await getConfig("autoCreateTicket");
            console.log("Falling back to 'autoCreateTicket' config branching");
        }

        if (config) {
            await buildAndInvokeOptionPayload(config, event);
            
            const webhookUrl = process.env.AUTOMATION_WEBHOOK_URL;
            if (!webhookUrl) {
                throw new Error("Missing 'AUTOMATION_WEBHOOK_URL' config or default WEBHOOK_URL");
            }

            await closeSlackSession(event);

            return successResponse(
                event,
                "Your request has been submitted successfully."
            );
        }

    } catch (err) {
        console.error("Router error:", err);

        if (err.name === "ResourceNotFoundException") {
            return {
                sessionState: {
                    ...event.sessionState,
                    dialogAction: {
                        type: "Delegate"
                    }
                }
            };
        }

        return failResponse(
            event,
            "Something went wrong."
        );
    }
};

/* ==========================================================
      BUILD GENERIC JIRA PAYLOAD
========================================================== */
async function buildAndInvokeOptionPayload(config, event) {
    const state = event.sessionState.sessionAttributes || {};
    
    const reportType = state.reportType || getSlotValue(event.sessionState.intent.slots, "reportType");

    const branching = config.branching === 'combined'
        ? config.branching
        : config.branching;

    const parsedParams = {};
    if (config.parameters) {
        try {
            const configParams = typeof config.parameters === "string"
                ? JSON.parse(config.parameters)
                : config.parameters;
            
            Object.keys(configParams).forEach(key => {
                parsedParams[key] = parsedParams[key] === undefined
                    ? configParams[key]
                    : parsedParams[key];
            });
        } catch (e) {
            console.error("Invalid parameters JSON:", e);
        }
    }

    const payload = {
        title: state.title || config.title,
        branching: branching,
        assignment: {
            assignee: parsedParams.assignee || undefined,
            projectParams: parsedParams.assignee || undefined
        },
        requestType: config.requestType,
        businessNotification: {
            description: config.description,
            additionsDetails: [],
            slackMessage: []
        },
        slack: {
            threadTs: state.slackThreadTs,
            channelId: state.slackChannelId,
        },
        user: state.slackUserId,
        email: state.email,
        atlassianAccountId: state.atlassianAccountId,
    };

    console.log("Payload compiled:", JSON.stringify(payload));
    const webhookToken = process.env.CREATE_TICKET_TOKEN;
    await invokeWebhook(payload, webhookToken);
}

/* ==========================================================
      DYNAMODB CONFIG LOOKUP
========================================================== */
async function getConfig(intentName) {
    const result = await ddb.send(
        new GetCommand({
            TableName: CONFIG_TABLE,
            Key: { intentName }
        })
    );
    return result.Item || null;
}

/* ==========================================================
      INVOKE DOWNSTREAM LAMBDA
========================================================== */
async function invokeReturn(functionName, event) {
    console.log(`Invoking Lambda: ${functionName}`);

    const response = await lambdaClient.send(
        new InvokeCommand({
            FunctionName: functionName,
            InvocationType: "RequestResponse",
            Payload: Buffer.from(JSON.stringify(event))
        })
    );

    if (!response.Payload) {
        throw new Error("No payload returned from functionName");
    }

    const payloadString = Buffer.from(response.Payload).toString("utf-8");
    const payload = JSON.parse(payloadString);
    return payload;
}

/* ==========================================================
      WEBHOOK CALLER
========================================================== */
async function invokeWebhook(payload, url) {
    console.log(`Calling webhook: ${url}`);
    
    const controller = new AbortController();
    const id = setTimeout(() => controller.abort(), 10000);

    try {
        const response = await fetch(url, {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
            },
            body: JSON.stringify(payload),
            signal: controller.signal
        });

        clearTimeout(id);

        console.log(`Webhook status: ${response.status}`);
        const responseText = await response.text();
        console.log(`Webhook response: ${responseText}`);

        if (!response.ok) {
            throw new Error(`Webhook error status: ${response.status}`);
        }
    } catch (err) {
        clearTimeout(id);
        throw err;
    }
}

/* ==========================================================
      SUCCESS RESPONSE
========================================================== */
function successResponse(event, message) {
    return {
        sessionState: {
            ...event.sessionState,
            dialogAction: {
                type: "Close"
            },
            intent: {
                ...event.sessionState.intent,
                state: "Fulfilled"
            }
        },
        messages: [
            {
                contentType: "PlainText",
                content: message
            }
        ]
    };
}

/* ==========================================================
      FAILURE RESPONSE
========================================================== */
function failResponse(event, message) {
    return {
        sessionState: {
            ...event.sessionState,
            dialogAction: {
                type: "Close"
            },
            intent: {
                ...event.sessionState.intent,
                state: "Failed"
            }
        },
        messages: [
            {
                contentType: "PlainText",
                content: message
            }
        ]
    };
}

/* ==========================================================
      MARK SESSION CLOSED
========================================================== */
async function closeSlackSession(event) {
    const threadTs = event.sessionState.sessionAttributes?.slackThreadTs;

    if (!threadTs) {
        console.log("No ThreadTs found - skipping cleanup");
        return;
    }

    await ddb.send(
        new UpdateCommand({
            TableName: process.env.EXPIRE_CONN_TABLE,
            Key: { threadTs },
            UpdateExpression: "SET #st = :status, #ts = :timestampStage + :ttl",
            ExpressionAttributeNames: {
                "#st": "status",
                "#ts": "ttl"
            },
            ExpressionAttributeValues: {
                ":status": "closed",
                ":timestampStage": Math.floor(Date.now() / 1000),
                ":ttl": 600
            }
        })
    );

    console.log("Session marked CLOSED");
    
    /* ==========================================================
          DELETE DOWNSTREAM SCHEDULE
    ========================================================== */
    try {
        await schedulerClient.send(
            new DeleteScheduleCommand({
                Name: `schedule-${threadTs}`
            })
        );
        console.log(`Deleted schedule: schedule-${threadTs}`);
    } catch (err) {
        if (err.name === "ResourceNotFoundException") {
            console.log("No standard schedule found");
        } else {
            throw err;
        }
    }
}

/* ==========================================================
      ATLASSIAN LOOKUP
========================================================== */
async function getAtlassianAccountFromSlackUser(slackUserId) {
    try {
        const response = await fetch(
            `https://slack.com/api/users.profile.get?user=${slackUserId}`,
            {
                headers: {
                    Authorization: `Bearer ${process.env.SLACK_BOT_TOKEN}`
                }
            }
        );

        const data = await response.json();
        if (!data.ok) {
            console.log("Slack lookup error:", data.error);
            return null;
        }

        const email = data.profile?.email;
        if (!email) {
            console.log("No email found for Slack profile");
            return null;
        }

        const jiraUserResponse = await fetch(
            `https://your-domain.atlassian.net/rest/api/3/user/search?query=${email}`,
            {
                headers: {
                    Authorization: `Basic ${Buffer.from(
                        `${process.env.ATLASSIAN_EMAIL}:${process.env.ATLASSIAN_API_TOKEN}`
                    ).toString("base64")}`,
                    Accept: "application/json"
                }
            }
        );

        const jiraUserData = await jiraUserResponse.json();
        if (Array.isArray(jiraUserData) && jiraUserData.length > 0) {
            return jiraUserData[0].accountId;
        }
        return null;
    } catch (err) {
        console.error("Failed to lookup Atlassian account from Slack user", err);
        return null;
    }
}

/* ==========================================================
      HELPER
========================================================== */
function getSlotValue(slots, slotName) {
    if (!slots || !slots[slotName]) {
        return "";
    }
    return slots[slotName].value?.interpretedValue || slots[slotName].value?.originalValue || "";
}

