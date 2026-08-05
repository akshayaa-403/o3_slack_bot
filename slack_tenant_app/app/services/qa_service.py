"""
Question-answering service. This is the ONLY place that knows which LLM
provider/model is in use — that detail never crosses into an API response,
log line visible to customers, or client-facing error message. Swapping
providers means changing Config.LLM_PROVIDER, nothing else.
"""
from app.config import Config
from app.db import store


def _call_llm(system_context: str, question: str) -> str:
    if Config.LLM_PROVIDER == "mock":
        return f"[mock answer] Based on your connected knowledge base: '{question}' -> handled."

    if Config.LLM_PROVIDER == "anthropic":
        import anthropic
        client = anthropic.Anthropic()
        resp = client.messages.create(
            model=Config.LLM_MODEL,
            max_tokens=500,
            system=system_context,
            messages=[{"role": "user", "content": question}],
        )
        return resp.content[0].text

    raise ValueError(f"Unknown LLM_PROVIDER: {Config.LLM_PROVIDER}")


def answer_question(tenant_id: str, question: str) -> str:
    intents = store.get_intents(tenant_id)
    context_snippets = [i.get("body", "") for i in intents.values()]
    system_context = (
        "You are a helpful assistant for this workspace. Use the following "
        "internal knowledge if relevant:\n" + "\n".join(context_snippets)
    )

    answer = _call_llm(system_context, question)
    store.log_conversation(tenant_id, question, answer)
    return answer


def regenerate_intents_from_conversations(tenant_id: str) -> dict:
    """Periodic job (e.g. weekly): rolls conversation history back into
    fresh intents so the tenant's knowledge base improves over time."""
    conversations = store.get_conversations(tenant_id)
    if not conversations:
        return {"regenerated": 0}

    new_intents = {}
    for i, convo in enumerate(conversations[-20:]):  # most recent batch
        intent_id = f"{tenant_id}_convo_{i}"
        new_intents[intent_id] = {"body": f"Q: {convo['question']} A: {convo['answer']}", "source": "conversation"}

    store.put_intents(tenant_id, new_intents)
    return {"regenerated": len(new_intents)}
