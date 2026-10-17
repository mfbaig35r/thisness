# Palantir, But Not Evil

There is a class of software that gets more valuable as the world gets messier.

Not because it stores more data.

Not because it has a better chat interface.

Because it creates a better model of reality.

That, more than anything else, is what I find interesting about Palantir.

Strip away the branding, government contracts, controversy, and enormous product surface area, and there is a very compelling technical idea underneath it:

> Take fragmented information from many different systems, determine what real-world things that information refers to, represent those things and their relationships explicitly, and give humans and software a coherent operational model to reason over.

I want to build an open version of that idea.

Not a Palantir clone.

Not another data platform.

And definitely not another "chat with your enterprise data" wrapper.

I want to build the foundational pieces of an **open, inspectable, reversible semantic layer for AI systems**.

One of the first pieces is something I've been calling **Thisness**.

---

## The problem isn't that we don't have enough data

Organizations are drowning in data.

The problem is that the same thing exists everywhere under different representations.

A company might appear as:

```text
International Business Machines Corporation
IBM
IBM Corp.
I.B.M.
```

A person might appear as:

```text
Mohammad Ali Khan
Mohammad A. Khan
M. A. Khan
M Khan
```

A contract might exist as:

```text
MSA_Final.docx
MSA_Final_FINAL.pdf
Signed Agreement.pdf
Scan_004183.pdf
```

And an amendment to that contract might look almost identical while being a **different legal object with a relationship to the original**.

Every enterprise system has versions of this problem.

CRMs.

ERP systems.

Contracts.

Invoices.

Emails.

Documents.

Data warehouses.

SharePoint sites.

Data lakes.

And now, increasingly, AI-generated structured information extracted from all of the above.

At some point, somebody has to answer a deceptively difficult question:

> Are these observations describing the same thing?

That is entity resolution.

And I increasingly think entity resolution is one of the missing foundational primitives for enterprise AI.

---

## From records to reality

Most software starts from records.

A row in Salesforce.

A PDF.

An invoice.

A database record.

A paragraph in a contract.

But a row is not a company.

A PDF is not a contract.

A string containing someone's name is not a person.

These are **observations** about things.

That distinction sounds philosophical until you start building systems around it.

Consider three records:

```text
CRM
----
Name: IBM
Domain: ibm.com


Invoice
-------
Vendor: IBM Corp.
Address: Armonk, NY


Contract
--------
Party: International Business Machines Corporation
```

You can leave these as three disconnected records.

Or you can resolve them to:

```text
          ┌──────────────────────────────┐
          │ Organization: org_123        │
          │                              │
          │ International Business       │
          │ Machines Corporation         │
          └──────────────┬───────────────┘
                         │
       ┌─────────────────┼─────────────────┐
       │                 │                 │
       ▼                 ▼                 ▼
  CRM record      Invoice record   Contract mention
     "IBM"          "IBM Corp."     full legal name
```

Now we have something much more useful.

We don't merely know that three strings are similar.

We have a durable claim that three observations refer to the same underlying entity.

And critically, we can preserve the evidence for that claim.

That is the idea behind Thisness.

---

## Thisness: identity as infrastructure

Thisness is intended to be a general-purpose open-source identity-resolution engine.

The important word there is not *entity*.

It's **identity**.

Its job is to take imperfect observations and determine what underlying things they refer to.

Conceptually:

```text
            Raw Observations
                    │
                    ▼
              Normalization
                    │
                    ▼
          Candidate Generation
                    │
                    ▼
                Evidence
                    │
                    ▼
               Resolution
                    │
                    ▼
                 Policy
                    │
       ┌────────────┼────────────┐
       │            │            │
       ▼            ▼            ▼
    Attach       Review       Create
       │
       ▼
 Stable Entity
```

But the important part is what **doesn't** disappear.

The original observation remains.

The evidence remains.

The resolution decision remains.

The model or rule version remains.

If a human reviewed it, that remains.

If the system later decides it was wrong, that remains too.

Identity becomes something the system can reason about **without pretending it is infallible**.

---

## Observations are not entities

This is probably the most important design decision in the system.

Imagine a contract contains:

> M. Khan

That is an observation.

The system might determine that it refers to:

```text
person_7281
```

whose current canonical representation is:

```text
Mohammad Ali Khan
```

But we never replace the observation.

Instead:

```text
"M. Khan"
   │
   │ observed in contract_184
   │
   ▼
Observation obs_991
   │
   │ evidence:
   │ - employer matches
   │ - phone matches
   │ - name compatible
   │
   ▼
Identity Membership
   │
   ▼
person_7281
```

Later, if we discover that the phone number had been reassigned and the match was incorrect, we can reverse the membership.

We don't have to reconstruct history.

Nothing was destructively collapsed.

This becomes extremely important when these identities feed downstream graphs and AI systems.

---

## The graph is where this gets interesting

Entity resolution by itself is useful.

But once stable identity exists, you can construct something much more powerful.

A graph of the world represented in your data.

For example:

```text
┌──────────────┐
│   Person A   │
└───────┬──────┘
        │ works_at
        ▼
┌──────────────┐
│ Company XYZ  │
└───────┬──────┘
        │ party_to
        ▼
┌──────────────┐
│ Contract 42  │
└───────┬──────┘
        │ covers
        ▼
┌──────────────┐
│ Supplier ABC │
└───────┬──────┘
        │ exposed_to
        ▼
┌──────────────┐
│   Risk 17    │
└──────────────┘
```

Behind each node might be hundreds or thousands of observations.

Behind each relationship might be evidence from many different sources.

But applications don't have to start from scratch every time.

They can begin with the resolved abstraction.

That is where I think the connection to large language models becomes especially important.

---

## LLMs don't need more context. They need better context.

A lot of AI architecture today is implicitly based on the idea that larger context windows solve knowledge problems.

I don't think they do.

A one-million-token context window is impressive.

It is not the same thing as a one-million-token **effective reasoning window**.

As context gets larger, the model has more information available, but it also has more information competing for attention.

If I want an LLM to understand a particular person across 10,000 legal documents, the naive architecture looks something like:

```text
                    10,000 documents
                            │
                            ▼
            retrieve vaguely relevant chunks
                            │
                            ▼
               maybe 50,000–200,000 tokens
                            │
                            ▼
                           LLM
                            │
                            ▼
"Please figure out who this person is and what matters."
```

The model is spending scarce reasoning capacity doing work that should have been performed upstream.

Identity.

Deduplication.

Relationship discovery.

Evidence aggregation.

Context selection.

Now compare that with a resolved graph.

```text
                    10,000 Documents
                            │
                            ▼
            Entity + Relationship Resolution
                            │
          ┌─────────────────▼─────────────────┐
          │ person_7281                       │
          │                                   │
          │ 143 document observations         │
          │ 8 contracts                       │
          │ 3 organizations                   │
          │ 11 material events                │
          │ 4 unresolved contradictions       │
          │ temporal history                  │
          └─────────────────┬─────────────────┘
                            │
                            ▼
                targeted context assembly
                            │
                            ▼
                           LLM
```

The model no longer has to discover the entity.

It gets to **reason about the entity**.

That is a completely different workload.

---

## The graph is a context compiler

I've started thinking about the semantic graph as something like a **context compiler for language models**.

Raw enterprise information might contain millions or billions of tokens.

But if we've already resolved:

- who the people are;
- which organizations are the same;
- what contracts correspond to which agreements;
- which amendments modify which contracts;
- what risks represent the same underlying risk;
- where an entity appears;
- what relationships exist among those entities;

then we can compile a much smaller representation for the model.

Instead of:

```text
everything we know
```

we provide:

```text
everything relevant to this thing
```

And importantly, we retain pointers back to the original source material.

So the abstraction does not replace evidence.

It organizes it.

---

## This changes the economics of context

LLM architecture has an economic dimension that gets surprisingly little attention.

Every unnecessary token costs something.

Not merely API dollars.

It costs:

- latency;
- attention;
- reasoning capacity;
- retrieval complexity;
- opportunity for distraction;
- probability of contradictions being missed.

Suppose the raw corpus contains:

```text
100,000,000 tokens
```

A question concerns one supplier.

Traditional retrieval might retrieve:

```text
75,000 tokens
```

spread across dozens of documents.

But a resolved identity graph might already know:

```text
Supplier: supplier_481

82 supporting observations
12 contracts
41 purchase orders
3 facilities
2 known aliases
1 parent organization
4 associated risks
```

The system can now construct a focused context package:

```text
2,500–10,000 tokens
```

and selectively drill into raw evidence only when needed.

The important optimization isn't merely fewer tokens.

It's **higher information density per token**.

---

## RAG tells you what looks relevant

This is also why I think entity resolution and retrieval solve different problems.

Vector search answers something like:

> What looks semantically related to this query?

Entity resolution answers:

> What observations refer to this actual thing?

Those are not interchangeable.

Consider:

```text
Apple Inc.
Apple Bank
Apple Records
Apple Hospitality REIT
```

Embeddings may correctly determine that they are semantically related.

But they are emphatically not the same entity.

This is why I don't want Thisness to become an embedding wrapper.

Embeddings should help find candidates.

Evidence and resolution policy should determine identity.

```text
         Vector Search
               │
               │ "these look related"
               │
               ▼
      Candidate Entities
               │
               ▼
       Identity Evidence
               │
    ┌──────────┴──────────┐
    │                     │
    ▼                     ▼
  MATCH               NOT MATCH
```

Similarity is useful.

Identity is stronger.

---

## The ontology isn't just a prettier graph

This is where the architecture starts resembling the interesting parts of Palantir.

Once identity exists, you can define a semantic representation of the world.

Not:

```text
table_384
column_17
document_481
row_2294
```

but:

```text
Person
Organization
Supplier
Contract
Facility
Risk
Product
Transaction
```

and:

```text
works_at
owns
supplies
party_to
amends
located_at
exposed_to
depends_on
```

That ontology becomes a shared language between:

- humans;
- analytics;
- applications;
- search systems;
- AI agents.

The underlying databases can change.

The documents can change.

The model can change.

The semantic objects remain comparatively stable.

---

## But the ontology must remain accountable

This is the part I think matters if you're trying to build this philosophy differently.

A semantic graph can become incredibly powerful.

It can also become dangerous if nobody understands how it arrived at its conclusions.

Every consequential statement should therefore retain provenance.

For example:

```text
Company A ─── subsidiary_of ───► Company B
```

should be able to answer:

```text
Why do we believe this?

Source:
    SEC filing 2026-04-03

Evidence:
    "Company A is a wholly owned subsidiary..."

Extractor:
    relationship_extractor_v4

Model:
    ...

Reviewed:
    analyst_17

Effective date:
    2026-03-01
```

Likewise:

```text
Observation 981 ─── refers_to ───► Person 441
```

should have an explanation.

The graph cannot become a collection of unexplained assertions generated by models.

---

## "Not evil" should be an architectural property

I say "Palantir but not evil" somewhat jokingly.

But there's a serious architectural principle underneath it.

If we're going to build systems that assemble increasingly comprehensive models of organizations and the people and things within them, then openness and accountability shouldn't just be marketing language.

They should be properties of the system.

That means things like:

**Inspectable resolution.** You can see exactly why two things were considered identical.

**Reversible identity.** A mistaken merge can be undone.

**Explicit uncertainty.** The system is allowed to say "I don't know."

**Source provenance.** Assertions retain pointers to their evidence.

**Portable data.** Your semantic model should not exist only inside a proprietary platform.

**Model independence.** GPT, Claude, Gemini, local models, probabilistic models, deterministic rules, and humans can all participate without becoming the architecture.

**Human authority.** High-impact decisions can require review or approval.

**Open interfaces.** Python, SQL, APIs, MCP, graph protocols.

**Self-hosting.** Organizations should be able to operate the identity layer inside their own environment.

Those aren't governance documents sitting next to the software.

They're engineering requirements.

---

## Thisness is only the first layer

Thisness should remain small.

Its job is identity.

```text
     OPEN SEMANTIC PLATFORM

┌──────────────────────────────┐
│    Agents / Applications     │
└───────────────┬──────────────┘
                │
┌───────────────▼──────────────┐
│     Semantic / Ontology      │
│                              │
│ People • Companies • Risks   │
│ Contracts • Products • etc.  │
└───────────────┬──────────────┘
                │
┌───────────────▼──────────────┐
│           Thisness           │
│                              │
│ observations                 │
│ evidence                     │
│ identity                     │
│ resolution memory            │
│ relationships                │
│ provenance                   │
└───────────────┬──────────────┘
                │
┌───────────────▼──────────────┐
│       Enterprise Data        │
│                              │
│ DBs • Docs • Email • APIs    │
│ Video • Code • Warehouses    │
└──────────────────────────────┘
```

Above Thisness can eventually sit an open ontology layer.

Above that, agentic workflows.

But if identity is wrong, everything above it becomes unreliable.

That's why I think this is the right place to begin.

---

## The weird examples are actually the important ones

One thing that changed my thinking while designing Thisness was testing the abstraction against use cases that have nothing to do with traditional customer deduplication.

Could the same engine resolve:

- biological species from morphological and genomic evidence?
- recurring action sequences across video?
- equivalent functions across codebases?
- risks expressed differently across contracts and supply-chain data?
- clauses representing the same legal obligation?

If the answer is yes, then the fundamental abstraction isn't really:

> customer matching.

It is:

> **resolution of identity under imperfect observation.**

The domain supplies the meaning of "same."

The engine supplies the machinery:

```text
observations
candidates
evidence
constraints
decisions
identity
relationships
memory
history
```

That distinction is what makes the architecture interesting to me.

---

## Where this could go

I don't think the future of enterprise AI is every application independently doing retrieval over raw documents.

That architecture throws away enormous amounts of previously learned structure every time a new question is asked.

Instead, I think increasingly capable systems will maintain persistent models of the environments they operate in.

They will know:

- what entities exist;
- how those entities are related;
- what has been observed about them;
- which claims conflict;
- how confidence has changed;
- where the source evidence lives.

Then an LLM doesn't have to reconstruct the world every time somebody asks a question.

It receives a useful representation of the world and spends its intelligence reasoning about it.

That is the piece of Palantir I find compelling.

And it's the part I think should exist as open infrastructure.

Not a black box.

Not an irreversible graph.

Not a giant proprietary ontology that owns your organization's understanding of itself.

Something smaller.

Composable.

Inspectable.

Reversible.

Model-agnostic.

And open.

Thisness is the first piece of that system.

And if we get the identity layer right, a lot of the rest becomes much easier.