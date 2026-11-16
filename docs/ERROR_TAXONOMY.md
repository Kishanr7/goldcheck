# What real text-to-SQL mistakes look like: 99 BIRD counterexamples, classified

**Data:** BIRD dev, official ChatGPT (gpt-3.5-turbo, 2023) baseline predictions. Held-out sample, seed 777: 100 of the 879 answers goldcheck marks as silently wrong. 99 shrank to a counterexample.
**Method:** one reader (the assistant) looked at question, documented hint ("evidence"), gold SQL, predicted SQL and the counterexample, and assigned one primary category. **Label: `ASSISTANT_TESTED` judgement, not independently reviewed.** Boundaries are fuzzy, especially between A and D.

## Result

| | Category | Count | What it means for "better models will fix it" |
|---|---|---:|---|
| A | **Data-encoding / definition miss**: wrong literal, letter case, code, date format or column, or ignored the documented meaning | **42** | Not raw SQL skill. The model did not know *this* database's conventions. Agents that look at the data first will reduce it. A counterexample row shows the real value at once. |
| D | **Gross misreading or hallucination**: wrong entity, invented ids or placeholders, answered a different question | 23 | Shrinks fast with better models. The counterexample proves it but adds little to understanding. |
| B | **SQL logic slip**: DISTINCT or duplicates, join fan-out, NULL handling, ties, LIMIT, grouping, date comparison | 14 | The classic "bad SQL" case, which better models erase. Also where clause blame is most precise. |
| C | **Gold answer questionable**: the benchmark's own SQL contradicts the question, or the prediction is arguably right | 11 | Does not go away with better models. A counterexample is how a human adjudicates who is wrong. |
| E | **Format or evaluation artifact**: extra columns, rounding, Yes/No vs code, concatenated names | 9 | Comparison-rule issue, not a model error. |

**Reading:** only about 14% are the generic SQL mistakes that model progress clearly removes. About 53% (A + C) depend on knowledge of one particular database or on a disputed answer key. That is where a few real rows are most useful. This is one model from 2023 on one benchmark. Newer models make fewer D and B errors, which raises the share of A and C among what is left but shrinks the total.

## Per question (BIRD question_id: category, reason)

A: 2 county vs district name · 118 status code 'A' instead of 'C' · 137 status code · 140 column A4 vs A2 · 170 operation code read as card type · 191 frequency 'W' vs documented code · 197 element case and bond code · 215 element case · 255 'single' vs '-' · 283 'Ca' vs 'ca' · 295 label text vs '+' · 314 'single' vs '-' · 363 format vs isStarter flag · 426 isOnlineOnly vs mtgoCode · 454 'Unknown' vs '*'/NULL · 473 wrong availability column · 505 language name order · 525 'Old School' vs 'oldschool' · 645 ignored documented threshold · 650 singular vs plural badge name · 658 popularity definition · 723 colour case · 778 power name case · 784 race stored as attribute · 857 name looked up in wrong table · 864 "finished" definition · 982 status code · 1007 milliseconds vs duration · 1025 season label format · 1111 date format · 1176 date format · 1179 description vs first date · 1220 documented value ignored · 1335 wrong column and case · 1370 position case · 1441 exact match vs LIKE and missing position · 1464 date format · 1467 category case · 1487 'Euro' vs 'EUR' · 1491 segment case · 1521 date and time format · 1528 segment case

D: 17 · 52 · 157 · 205 · 218 · 447 · 513 · 574 · 620 · 635 · 671 · 767 · 959 · 971 · 986 · 1103 · 1141 · 1170 · 1217 · 1239 · 1350 · 1455 · 1458

B: 10 MAX subquery vs ORDER BY on ties/NULL · 63 extra joins drop rows · 352 different denominator · 472 missing NOT NULL · 592 GROUP BY/HAVING returns per-group rows · 830 missing LIMIT · 856 missing DISTINCT · 947 date string vs year comparison · 961 NULLs sort first · 1053 missing DISTINCT · 1077 scalar subquery takes one of two matches · 1136 filter inside vs outside MAX · 1166 missing NOT NULL · 1444 groups by id

C: 16 gold filters a different county than asked · 214 gold semantics contradict question · 383 gold counts join duplicates · 441 gold needs an unrelated join · 443 same · 816 gold returns join duplicates · 945 hint contradicts question · 1023 gold ignores half the condition · 1027 gold joins on the wrong key · 1406 gold takes MAX over all categories · 1530 "best selling" by single transaction

E: 221 · 289 · 577 · 921 · 958 · 1011 · 1137 · 1437 · 1469
