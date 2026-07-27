"""Automated intent pipeline: tickets -> intent sheet -> Lex.

Two agents, both a deterministic pipeline that calls an LLM only for the steps
that need judgement (extraction, labelling, writing utterances/responses):

  Agent 1  (agents.ingest)      pull tickets from a client's platform, LLM-extract
                                {request, resolution, category} -> interaction corpus
  Agent 2  (agents.synthesize)  cluster the corpus, LLM-label each cluster into an
                                intent (name, utterances, response, static/lambda),
                                emit the intent sheet the Lex loader consumes, and
                                (optionally) load it.

Everything depends only on two interfaces so it is testable with no API keys and
the real provider/platform drops in later:
  - LLMClient       (agents.llm)         provider chosen later; MockLLMClient for tests
  - TicketConnector (agents.connectors)  Jira first; MockTicketConnector for tests
"""
