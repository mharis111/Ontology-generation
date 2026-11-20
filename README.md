# Ontology generation with existing vocabularies — v4.0.1

Generate one ontology file from your competency questions (CQs), LOT ORSD context and prompt instructions. Supports local Ollama models and OpenAI. The default is **one joint ontology from all four supplied CQs**.

This revision uses **existing vocabulary terms only**. It adds Schema.org to Dublin Core Terms, CiTO, DCAT and SKOS, and removes local class/property generation. For example, the LLM can choose `schema:ScholarlyArticle`, `schema:Person`, `schema:author`, `cito:cites` and `dcterms:identifier`; it cannot create `lumis:Article`, `lumis:Author` or an invented `schema:Publication`.

**v4.0.1 fix:** the provider response schema now separates resource-valued and literal-valued property uses with disjoint `anyOf` branches. A literal property such as `dcterms:identifier` requires `value_classes: []` and a nonempty `datatypes` list. Local validation remains strict. The prompt includes identifier/authorship examples, and a mismatch produces a repair message with the exact usage index and the appropriate shape. The application never silently converts a class-valued identifier into a literal.

When upgrading, extract the new complete folder and **restart the notebook kernel** before running all cells. Check that the displayed engine version is **4.0.1**. Your existing CQ/ORSD/settings file formats are unchanged; copy your edits into the new folder if needed, keeping the updated prompt instructions.

## Start here

1. Extract this **entire ZIP**, keeping its folders together. Requires Python 3.10+.
2. On the machine running Ollama, run `ollama serve` if needed, then `ollama pull qwen3:4b`.
3. Open **Generate_Ontology.ipynb** from the extracted project folder and run its cells. Change model/provider settings in `settings.json` before generation.
4. A successful run saves **outputs/ontology.owl**. Open it in Protégé or another RDF/OWL tool.

Alternatively, from the project folder:

```bash
python -m pip install -r requirements.txt
python run.py --preview
python run.py
```

`--preview` validates inputs and prints the initial prompt without calling a model or writing an ontology. If Jupyter runs on another machine, `localhost` refers to that machine: configure `ollama_base_url` accordingly. Ollama must support JSON-schema structured output and your chosen model's thinking setting. For models that do not support `think`, set `ollama_think` to `null`.

## Required editable input files

| File | What you edit |
|---|---|
| `inputs/cqs.json` | CQ IDs, questions, expected answers and optional notes/use-case IDs |
| `inputs/orsd.json` | Purpose, scope, use cases, exchange fields, functional requirements (statements/tables), quality requirements, terminology and modelling decisions |
| `prompts/ontology_instructions.md` | Instructions explaining how to model those requirements |
| `vocabularies/catalogue.json` | Approved existing term IRIs, definitions, usage guidance and curated source relationships |
| `settings.json` | Provider/model, generation mode, file paths, CQ selection and output destination |

All files are loaded again when generation starts; nothing needs to be copied into a notebook cell. Relative paths resolve from `settings.json`, independent of the notebook's working directory. The ORSD JSON structure is this project's representation of LOT requirements; it is not an official LOT JSON standard. The supplied ORSD is an editable pilot and needs your domain review.

`cq_ids: null` selects every CQ. To select only the original three, set:

```json
"cq_ids": ["CQ_001", "CQ_002", "CQ_003a"]
```

`CQ_003b` is the separate topic/keyword-filtered dataset question. Requirements with `applies_to` are scoped to selected CQs; shared context remains available.

## How vocabulary reuse works

The prompt receives the catalogue's meanings and usage guidance. The LLM returns selected term IDs and proposed property uses, **not raw Turtle or local term definitions**. Python validates them and builds the RDF graph. An invented or unapproved term triggers a bounded repair attempt; it is never silently replaced or turned into a new class.

For example, an identifier use on a work is `{"property":"dcterms:identifier","on_class":"schema:CreativeWork","value_classes":[],"datatypes":["string"]}`. `on_class` identifies the entity that has the identifier. Its identifier value is literal; it is not a link to a Person, Dataset or Identifier class. An entity-valued property such as `schema:author` uses `value_classes` and an empty `datatypes` list instead. A complete LLM response includes these records inside `property_uses`, along with selected terms, notes and unresolved requirements.

| Information | Available existing terms |
|---|---|
| Scholarly articles | `schema:ScholarlyArticle` → `schema:Article` → `schema:CreativeWork` |
| Other documented works | `schema:Book`, `schema:Report`; `schema:CreativeWork` is broader than publication |
| Authors and authorship | `schema:Person` or `schema:Organization`, connected by `schema:author` |
| Citations | `cito:cites`; optional published inverse `cito:isCitedBy` |
| Identifiers | `dcterms:identifier` with literal values in this pilot |
| Dataset discovery | `dcat:Catalog`, `dcat:dataset`, `dcat:Dataset` |
| Topics and keywords | `skos:Concept`, `skos:prefLabel`, `dcterms:subject`/`dcat:theme`, `dcat:keyword` |

The catalogue contains **26 curated terms**, not the complete contents of these vocabularies. This version does not automatically download or search entire vocabularies. To approve additional existing terms, edit the catalogue with their exact IRIs, kind, value kind, definitions, source URL/version and optional verified source relationships. Never add a fictional term merely to satisfy a CQ. Native hierarchies and catalogue `domain`/`range` constraints must reference approved classes. Schema.org's expected value types are guidance, not automatically global OWL restrictions.

For this pilot, dataset discovery is modelled as listings in a **federated metadata catalogue**. An arbitrary source RDF graph is not automatically a `dcat:Catalog`. Endpoint routing, named-graph provenance and source-data mappings remain application work; revise the ORSD/catalogue if those are ontology requirements. Generic identifier strings do not establish PID schemes, uniqueness or identity resolution. Citation direction, scholarly result types and keyword matching still need explicit query design.

## What is in the single output?

The default `.owl` file is RDF/XML containing:

- An ontology header with CQ text, modelling notes, unresolved requirements and compact run metadata/input hashes.
- Selected existing class/property IRIs with labels, definitions and curated source relationships. Dependencies such as native superclasses are included automatically. Whole external ontologies are not imported.
- Anonymous **SHACL usage shapes in the same file**, recording where properties are intended to be used and what value types are allowed. Person **or** Organization is represented as alternatives. These shapes introduce no named local classes or properties.

Source properties are not assigned new global domain/range restrictions from project-specific usage. Some general RDF properties, including Schema.org properties, are declared with OWL object/datatype property kinds for the selected usage profile; those are application typing decisions, not a claim that the source vocabulary publishes identical OWL axioms. The result is a draft ontology/application profile, not a complete copy or semantic validation of the source ontologies.

Protégé can open the OWL/RDF vocabulary. Its OWL reasoner does **not** enforce the embedded SHACL constraints; use a SHACL validator separately with example data. Shapes have no required-value/cardinality constraints and no instances. Syntactic acceptance does **not** establish CQ coverage, logical consistency, correct domain modelling or FAIR quality. Evaluate these separately with queries, test data, reasoning and expert review.

Change `output.file` to `outputs/ontology.ttl` for Turtle instead. Each successful run writes **one** configured ontology file. The default fixed path is replaced on successful reruns, avoiding accumulated output folders. Set `output.overwrite` to `false` to protect an existing file. Generation failures leave the previous successful file untouched; check the returned status, not just whether an old file exists. If requirements are explicitly unresolved, a draft is saved with status `needs_review` and the reasons in its comments.

There are no manifest/CSV/raw-response/SHACL companion files. Prompts, attempts and provider diagnostics stay in the Python result in memory for troubleshooting. Required inputs and code are never treated as generated output.

## Modes and providers

Set `generation.mode` in `settings.json`:

- `joint` (default): all selected CQs and scoped ORSD in one generation task; one coherent output.
- `independent`: separate CQ calls without prior generated content, combined into one output. Shared property/class value alternatives are unioned, which may broaden constraints and needs review.
- `incremental`: each CQ receives the prior profile. Additions are accepted; changing an earlier usage constraint is rejected. Use joint mode for redesign. A failed step blocks later steps.

All units must pass structural/reference validation before the combined ontology is saved. Structural repair attempts are bounded by `max_syntax_repairs`; incomplete provider output is rejected with diagnostic information. No model is guaranteed to generate a semantically correct profile.

For OpenAI, set `provider` to `openai` and `model` to a model available to your account supporting Chat Completions JSON output and `max_completion_tokens`. The notebook/CLI prompts privately for `OPENAI_API_KEY` if absent. Do not put keys in input files. The adapters use Python's standard HTTP library; no provider SDK is required. Unsupported model parameters should be adjusted in settings. Runtime generation requires your local Ollama server or network access/API credentials for OpenAI.

## Project files and migration

- `ontology_engine.py`: prompts, mode orchestration, validation/repairs, atomic single-file output.
- `vocabulary.py`: approved-term selection, semantic usage structure and RDF/SHACL serialization.
- `requirements_io.py`: runtime file loading and CQ/ORSD validation.
- `providers.py`: native Ollama/OpenAI adapters retained from the supplied project.
- `run.py`: command-line entry point; `Generate_Ontology.ipynb`: notebook entry point.
- `tests/test_project.py`: offline regression tests. Run `python -m unittest discover -s tests -v`.

Use the new project as a complete folder rather than copying individual v4 modules into v3. Removed configuration includes local namespace creation and local-term fallback. The response format is now reuse-only `elements`; old raw Turtle/`{"turtle": ...}` and local class/property JSON responses are not accepted. Model settings are separate from requirements. No demo generation or pre-generated ontology is included.

See `TESTING.md` for verification and its limits. Vocabulary references: [Schema.org](https://schema.org/), [Dublin Core Terms](https://www.dublincore.org/specifications/dublin-core/dcmi-terms/), [CiTO](https://sparontologies.github.io/cito/current/cito.html), [DCAT 3](https://www.w3.org/TR/vocab-dcat-3/), [SKOS](https://www.w3.org/TR/skos-reference/), [SHACL](https://www.w3.org/TR/shacl/).
