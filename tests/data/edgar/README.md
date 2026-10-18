# EDGAR people fixture

A small slice of real data for tests (design 09): Section 16 insiders as staged from an edgar-persons-pool store (classifier version 2): names, former names, CIKs, issuers, titles, role flags, filing dates, city and state. No street addresses or postal codes.

Contains information from SEC EDGAR Section 16 filings (Forms 3, 4 and 5). EDGAR filings are records of the US government; the SEC is credited as a courtesy.

Rebuild with `python -m thisness.benchmark edgar-fixture`.
