# Golden prompt set (eval harness, task #12)

One initial_prompt per file. To run an eval: create a project per prompt
(New App sheet or chat Home), let it run, then:

    python3 orchestrator.py --eval-report <slug1> <slug2> ...

Scores come from artifacts every run already writes (verify, adherence,
visual QA, UI crawl, design lint). Compare reports before/after any config
change. The harness never launches builds itself — launching spends quota
and stays a human decision.
