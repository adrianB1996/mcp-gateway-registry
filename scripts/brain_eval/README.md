# Brain eval (Phase 0)

Tooling for Phase 0 of the knowledge-graph search plan in [docs/design/knowledge-graph-search.md](../../docs/design/knowledge-graph-search.md). Phase 0 measures the problem before anyone builds a fix.

| File | What it does |
|---|---|
| `measure_deployment.py` | Reads a running registry's tool lists and MCP audit records and writes a JSON report. It runs in the pilot, once real users exist |
| `deployment_stats.py` | The analysis behind that report, with no I/O |
| `metrics.py` | NDCG@10, recall@10, MRR, top-1 hits, task completion and a paired bootstrap interval, shared by every eval arm |
| `corpus.py` | Builds the frozen corpus from the domain files: generic colliding tools, tasks, lookups, collision cases and filler servers |
| `build_corpus.py` | Writes `tests/fixtures/brain_eval/corpus.json` and `manifest.json`, and checks they are up to date |

The fake MCP servers and the harness that runs each arm against the corpus come next.

## The corpus

The corpus lives in `tests/fixtures/brain_eval/`. The hand-written part is in `domains/`, one YAML file per business domain. Each file defines its servers and their tools, its skills, its goals with three phrasings each, and lookup queries with their expected answers. Tasks come from the goals, so each task's correct tools are the tools its goal lists.

The builder adds what is better generated than written:

- every domain server gets `get_item`, `list_items` and `search` with identical descriptions, so only server context tells them apart;
- each phrasing of a goal becomes a task. Every third goal stays entirely in the hidden half, and the other goals put two phrasings in the tuning half and one in the hidden half;
- the two billing servers expose the same tools, and their goals produce one task per user group with different correct tools, for the same-domain collision test;
- a tool-name lookup for every tool whose name is unique, and a `get_item` and `search` collision case for every domain server;
- filler servers that pad the corpus to 1,000 or 10,000 tools. No task or lookup ever expects a filler tool.

| Part | Count |
|---|---|
| Domain servers | 22 |
| Tools before padding | 167 |
| Skills | 22 |
| Goals | 68 |
| Tasks (tuning, hidden) | 98, 124 |
| Lookups (tool capability, server purpose, skill, tool name) | 33, 33, 33, 97 |
| Collision cases | 40 |

After editing a domain file, rebuild and commit both files:

```bash
uv run python scripts/brain_eval/build_corpus.py
uv run python scripts/brain_eval/build_corpus.py --scale 1000 --scale 10000 --padded-dir /tmp/brain-eval
```

The padded corpora are not committed. The builder makes the same files from the same seed, and `manifest.json` records their SHA-256 so a run can show it used the same corpus. A unit test fails when `corpus.json` or `manifest.json` no longer matches the domain files.

## Measuring a deployment

The design runs this in the pilot, on real users' traffic. Against a local stack it only describes your own test calls, which is useful to check the script works and says nothing about real usage.

```bash
# Inside a registry container, where the registry environment is complete
uv run python scripts/brain_eval/measure_deployment.py --output brain-report.json

# From anywhere that can reach the database (password from DOCUMENTDB_PASSWORD)
DOCUMENTDB_PASSWORD=... uv run python scripts/brain_eval/measure_deployment.py \
    --host localhost --username admin --direct-connection --output brain-report.json

# Amazon DocumentDB
DOCUMENTDB_PASSWORD=... uv run python scripts/brain_eval/measure_deployment.py \
    --storage-backend documentdb --host docdb.cluster-xxxx.us-east-1.docdb.amazonaws.com \
    --username admin --tls --tls-ca-file global-bundle.pem --output brain-report.json
```

With `--host` or `MONGODB_CONNECTION_STRING` set, the script connects directly and needs none of the registry's secrets. Without either, it loads the registry's own settings, which refuse to start unless `SECRET_KEY` and the other required secrets are set, so run it inside a registry container in that case. Pass `--namespace` when the deployment sets `DOCUMENTDB_NAMESPACE`.

The script needs MCP audit records, which the auth server writes only when `AUDIT_LOG_MONGODB_ENABLED` is true. They expire after `AUDIT_LOG_MONGODB_TTL_DAYS` (7 by default), so `--days` cannot usefully go past that. If the window holds no tool calls, the script exits with status 1 and says why.

`uv run python scripts/brain_eval/measure_deployment.py --help` lists every option.

## Reading the report

The report holds counts and tool names. It never contains a username.

| Field | Question it answers |
|---|---|
| `collisions.shared_tool_share` | What share of tools has a name that another server also uses. This is how much Phase 1 has to fix |
| `collisions.shared_names_with_identical_description` | How many shared names also share a description, so only server context can separate them |
| `identity.calls_without_user_share` | What share of calls comes from a token that names no user. The brain cannot learn from these |
| `identity.top_caller_share` | What share of calls comes from the busiest caller. A high value suggests a shared service token |
| `searches.followed_by_backend_call_share` | How often a search leads to a tool call. If this is low, searches give the brain little to learn from |
| `searches.backend_calls_after_search_share` | What share of tool calls follows a search. If agents call tools without searching first, the brain sees little |
| `searches.repeated_without_call_share` | How often a search is followed quickly by another search, a sign the first answer did not help |
| `episodes.multi_server_share` | What share of episodes calls tools on two or more servers |
| `repeats.personal_repeat_share` | How often a caller reuses a set of tools they used before. This is what personal memory would return |
| `repeats.shared_tool_sets` | How many exact tool sets at least `--shared-min-users` users share. This is what shared use cases would build on |

The design stops early if callers rarely repeat a goal. `repeats.personal_repeat_share` and `repeats.shared_tool_sets` are the numbers to read for that decision.

## Limits

- An audit record proves a call was allowed. The auth server writes it before nginx forwards the request, so it cannot say whether the call worked.
- Audit records hold no query text, so the report treats "the same goal" as "the same set of tools". Two different goals that happen to use one tool count as a repeat, and one goal reached with different tools does not.
- `repeats.shared_tool_sets` compares tool sets exactly, so it undercounts shared goals.
- An episode is a caller's run of calls with no gap longer than `--episode-gap-minutes`. A caller who runs two tasks at once produces one episode.

## Tests

```bash
uv run pytest tests/unit/brain_eval --no-cov
```
