from backend.rag.knowledge_base import build_knowledge_base, search_knowledge_base

build_knowledge_base()

# What the agent will really search with: the stated causes from disruptions.json
real_causes = {
    "Marco (should be: technical problems)": "Technical fault: hydraulic system issue found during the pre-flight inspection. Aircraft grounded for repairs.",
    "Sophie (should be: weather / air traffic)": "Severe thunderstorms over Paris Charles de Gaulle. Air traffic control has suspended departures.",
    "Lukas (should be: own staff strike)": "Industrial action by Aurora Airways cabin crew. Insufficient crew available to operate the flight.",
    "Ana (should be: external strike)": "Strike by French air traffic controllers. The authorities have ordered airlines to reduce departures from Paris airports.",
    "Emily (should be: previous flight + technical)": "Late arrival of the inbound aircraft, held for an unscheduled technical inspection at its previous airport.",
    "Paolo (should be: bird strike)": "Bird strike on landing during the aircraft's previous flight. Aircraft grounded for a mandatory inspection.",
}

# Different wording, to check the search goes by meaning and not exact words
paraphrases = [
    "the pilots walked out because of a pay dispute",
    "the plane hit a goose on landing",
    "French controllers refused to work",
]

print("=== Real stated causes ===")
for label, cause in real_causes.items():
    print(f"\n{label}")
    for passage in search_knowledge_base(cause):
        print(f"   {passage['relevance']:.2f}  {passage['source']} / {passage['section']}")

print("\n=== Paraphrases ===")
for question in paraphrases:
    print(f"\nQ: {question}")
    for passage in search_knowledge_base(question):
        print(f"   {passage['relevance']:.2f}  {passage['source']} / {passage['section']}")