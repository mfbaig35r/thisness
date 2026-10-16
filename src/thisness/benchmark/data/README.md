# Benchmark data

`wfo_fixture.csv.gz`: every species-level name (accepted, and synonyms with the accepted
taxon each belongs to) in three plant families, Pinaceae, Betulaceae and Juglandaceae,
from the **World Flora Online Plant List 2026-06**, as published on ChecklistBank
(dataset 2004). The data is dedicated to the public domain under **CC0 1.0**; we credit
it as a courtesy:

> The World Flora Online Consortium et al. World Flora Online Plant List 2026-06.
> https://www.worldfloraonline.org, via ChecklistBank (https://www.checklistbank.org/dataset/2004).

Rebuild with `python -m thisness.benchmark wfo-fixture`. Other families are fetched on
demand and cached under `~/.cache/thisness/wfo`.

`edgar/`: about 150 SEC Section 16 insiders (directors, officers, holders of more than
10%) with their names, former names, CIKs, issuers, titles, role flags, filing dates,
city and state, for the people benchmark's tests (design 09). No street addresses or
postal codes. EDGAR filings are records of the US government; we credit the SEC as a
courtesy. Rebuild with `python -m thisness.benchmark edgar-fixture` from data staged by
`edgar-stage`.
