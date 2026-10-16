# Sequence diagrams

Four flows, in the order a run executes them. Component names match [system-design.md](system-design.md).

## 1. Seeds from a topic (topic mode)

```mermaid
sequenceDiagram
    autonumber
    actor User
    participant CLI
    participant SF as Seed Finder
    participant LLM as LLM (chat)
    participant SRC as Sources<br/>(OpenAlex, arXiv, Crossref, DBLP)
    participant EMB as Embedding API
    participant DB as Paper Store

    User->>CLI: run --topic "..." --seeds N
    CLI->>SF: find_seeds(topic, N, SEED_POOL_MAX)
    SF->>LLM: expand topic into search queries
    LLM-->>SF: queries
    par search every source
        SF->>SRC: search(query) for each query
        SRC-->>SF: candidate records
    end
    SF->>SF: merge and deduplicate by DOI / arXiv id / title
    SF->>EMB: embed topic + candidate title+abstract (batched)
    EMB-->>SF: vectors
    SF->>SF: rank by cos(topic, paper), keep top SEED_POOL_MAX
    SF->>LLM: pick the N most relevant papers given topic and abstracts
    LLM-->>SF: N seeds with reasons
    SF->>DB: store run, pool, seeds and pick reasons
    SF-->>CLI: seeds
```

## 2. Seeds from a PDF folder (PDF mode)

```mermaid
sequenceDiagram
    autonumber
    actor User
    participant CLI
    participant SI as Seed Importer
    participant SRC as Sources (OpenAlex, Crossref, arXiv)
    participant DB as Paper Store

    User->>CLI: run --seeds-dir ./seeds
    CLI->>SI: import(folder)
    loop each PDF
        SI->>SI: extract text, DOI, arXiv id, title
        alt DOI or arXiv id found
            SI->>SRC: get_by_id(id)
        else title only
            SI->>SRC: search(title), accept if titles match closely
        end
        alt resolved
            SRC-->>SI: ids, abstract, graph links
        else not resolved
            SI->>SI: local seed: abstract from PDF text, not expandable
        end
        SI->>DB: store seed
    end
    SI-->>CLI: seeds
```

## 3. Snowball with live distribution monitoring

```mermaid
sequenceDiagram
    autonumber
    participant CE as Crawl Engine
    participant SRC as Sources
    participant EMB as Embedding API
    participant DB as Paper Store
    participant DM as Distribution Monitor
    participant LLM as LLM judge
    participant MS as Metrics stream / dashboard

    CE->>SRC: sample random papers (null distribution)
    SRC-->>CE: random abstracts
    CE->>EMB: embed seeds + null sample
    EMB-->>CE: vectors
    CE->>DM: init(seed vectors, null scores)
    DM-->>CE: initial thresholds (tau_rel, tau_core)
    loop until all seeds given up, global stop or budget cap
        CE->>CE: pop best paper from each active seed's frontier
        par backward and forward
            CE->>SRC: references(paper)
            CE->>SRC: citations(paper)
        end
        SRC-->>CE: new records (failed calls queued for retry)
        CE->>SRC: fill missing abstracts (batched)
        CE->>EMB: embed title+abstract of new papers (batched)
        EMB-->>CE: vectors
        CE->>DB: store papers, edges, discoveries, per-seed cosines
        CE->>CE: queue papers with score >= tau_drop on the finder seed's frontier (expanded only if >= tau_rel)
        CE->>DM: observe(new scores, seed, direction, depth)
        opt every K new papers
            DM-->>CE: probe papers spread over the score range
            CE->>LLM: judge probe papers (title + abstract vs topic)
            LLM-->>CE: relevant yes/no
            CE->>DM: labels
            DM->>DM: refit thresholds (LLM calibration, else mixture), knee, yield, drift, capture-recapture
            DM->>MS: metrics snapshot
            DM-->>CE: thresholds, seeds to give up, stop?
            CE->>DB: log decision events
        end
    end
    CE->>DB: assign segments (core / candidate / boundary / discard)
```

## 4. LLM validation of the selected segment

```mermaid
sequenceDiagram
    autonumber
    participant VA as LLM Validator
    participant DB as Paper Store
    participant LLM as LLM (chat)
    participant RP as Reporter

    VA->>DB: load core + candidate papers
    VA->>DB: draw stratified sample from boundary + discard
    loop batches, in parallel
        VA->>LLM: is this paper relevant to the topic? (title + abstract)
        LLM-->>VA: score, label, reason
        VA->>DB: store judgment
    end
    VA->>VA: AUC, Spearman, precision@k, per-seed AUC
    VA->>VA: isotonic calibration cosine -> P(relevant), choose cut for target recall
    alt cosine ranking weak (AUC < 0.8)
        VA->>VA: rank by calibrated cosine combined with LLM score
    end
    VA->>RP: final list + validation report
    RP->>DB: export papers table (CSV / XLSX) and report
```
