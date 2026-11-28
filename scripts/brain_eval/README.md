# Brain eval (Phase 0)

Tooling for Phase 0 of the knowledge-graph search plan in [docs/design/knowledge-graph-search.md](../../docs/design/knowledge-graph-search.md). Phase 0 measures the problem before anyone builds a fix.

| File | What it does |
|---|---|
| `measure_deployment.py` | Reads a running registry's tool lists and MCP audit records and writes a JSON report. It runs in the pilot, once real users exist |
| `deployment_stats.py` | The analysis behind that report, with no I/O |
| `metrics.py` | NDCG@10, recall@10, MRR, top-1 hits, task completion and a paired bootstrap interval, shared by every eval arm |
| `corpus.py` | Builds the frozen corpus from the domain files: generic colliding tools, tasks, lookups, collision cases and filler servers |
| `build_corpus.py` | Writes `tests/fixtures/brain_eval/corpus.json`, `manifest.json` and one SKILL.md per skill, and checks they are up to date |
| `fake_servers.py` | Serves every corpus server as a fake MCP server at `/<server>/mcp` and logs each call it receives |
| `harness.py` | Registers the corpus in a registry, runs the lookups and collision queries through search, and scores them |
| `run_retrieval.py` | The command that runs the harness against a local registry and writes the baseline report |
| `task_runner.py` | Runs an agent on each task, reads the calls each task added to the fake servers' log, and scores them |
| `run_tasks.py` | The command that runs the eval agent from `agents/agent.py` on every task and writes the whole-task report |

## Running the baseline locally

The baseline needs the local Docker Compose stack, because search uses the registry's embedding model. It runs on the CPU, so the GPU stays free for the whole-task runs.

1. Start the stack with `./build_and_run.sh` and check `curl -s -o /dev/null -w '%{http_code}\n' http://localhost/health` returns 200.
2. Start the fake servers in a second terminal: `uv run python scripts/brain_eval/fake_servers.py`. The registry checks each registered server's health, so it has to reach them.
3. Save an admin token from the registry UI's "Get JWT Token" button to `.token` at the repo root.
4. Run the baseline:

```bash
uv run python scripts/brain_eval/run_retrieval.py --arm A \
    --skill-base-url https://github.com/OWNER/REPO/blob/BRANCH/tests/fixtures/brain_eval/skills \
    --output baseline-core.json
```

The registry runs in Docker and reaches the fake servers through `--fake-base-url`, which defaults to `http://host.docker.internal:9100`. Docker Desktop defines that name. On Linux it usually does not, so bind the fake servers to the Docker bridge address and pass the same address to both commands:

```bash
uv run python scripts/brain_eval/fake_servers.py --host 172.17.0.1 --allowed-host 172.17.0.1:9100
uv run python scripts/brain_eval/run_retrieval.py --arm A --fake-base-url http://172.17.0.1:9100 --skip-skills
```

Skills are registered by the URL of their SKILL.md, because the registry fetches it and its SSRF guard refuses local addresses. Point `--skill-base-url` at the `tests/fixtures/brain_eval/skills` directory of a pushed branch. For a private repository, put a GitHub token in an environment variable and pass its name with `--skill-token-env GITHUB_TOKEN`; the token never goes on the command line. Without either, pass `--skip-skills`, and the report leaves the skill category out instead of scoring it as zero.

For the scale runs, build the padded corpus, serve its filler servers too, and point the harness at it:

```bash
uv run python scripts/brain_eval/build_corpus.py --scale 1000 --padded-dir /tmp/brain-eval
uv run python scripts/brain_eval/fake_servers.py --corpus /tmp/brain-eval/corpus_1000.json --include-padding
uv run python scripts/brain_eval/run_retrieval.py --arm A --skip-skills \
    --corpus /tmp/brain-eval/corpus_1000.json --output baseline-1000.json
```

The report holds each category's mean NDCG@10, recall@10, MRR and top-1 hit rate, the collision ladder, and every query's ranked results, so Phase 1 can be compared with it query by query. It records the corpus file's SHA-256 and warns when the file matches nothing in `manifest.json`.

Two limits apply to the collision ladder. The search API returns at most 50 results, so a right answer ranked below 50th scores as a miss. Each level is scored by keeping only that level's servers in one ranked list. That matches a registry holding only those servers as long as search scores each document on its own, which holds for the current vector and keyword scoring.

## Running the whole tasks

The whole-task runs give each task's text to the eval agent in `agents/agent.py`. The agent searches the registry, calls tools through the gateway, and the fake servers log each call. A task counts as complete when every tool it expects was called. Extra calls do not fail it, and the report counts them separately.

The log decides the score, not the agent's own account, because a call only counts if it reached a server through the gateway. Tasks run one at a time, so every line a task adds to the log belongs to that task, and nothing else may call the fake servers during a run.

1. Do steps 1 to 3 of the retrieval baseline, and run it once so the corpus is loaded.
2. Start the fake servers with the log the runner reads, `brain-eval-calls.jsonl` in the directory you run from by default. Pass the same file to both with `--log` and `--call-log` to use another.
3. Pull the model and check Ollama answers: `ollama pull qwen3:8b`, then `ollama list`. The agent reads `OLLAMA_HOST` and defaults to `http://127.0.0.1:11434`.
4. Check the setup on a few tasks, then run them all:

```bash
uv run python scripts/brain_eval/run_tasks.py --split tuning --limit 5
uv run python scripts/brain_eval/run_tasks.py --arm A --output tasks-A.json
```

The Claude confirmation pass cannot run at temperature 0, so it runs three times and the report gives each repeat's completion:

```bash
uv run python scripts/brain_eval/run_tasks.py --provider anthropic --model claude-opus-5-5     --repeats 3 --output tasks-A-claude.json
```

It reads the key from `ANTHROPIC_API_KEY`. Each task has a time limit (`--timeout`, 300 seconds by default) and a step limit (`--max-steps`, 25 by default). A task that hits either still scores the calls it made.

The report holds completion overall, per split, per user group, per goal, and for hidden tasks by whether tuning saw their goal. Each task keeps the tools the servers saw called, the expected tools it missed, the agent's search queries and requested calls, and any error, so two arms can be compared task by task. For the Ollama provider the report records the model's digest.

The runner gives the agent its own short system prompt instead of `agents/system_prompt.txt`. With the stock prompt qwen3:8b answers tasks in prose without searching. The report records the prompt text.

Tool lookups and collision queries search with `entity_types` of `mcp_server` and `tool` together. The registry keeps tools inside their server's document, so asking for `tool` alone returns nothing.

The registry and the auth server both refuse private upstream addresses. Set `SSRF_ALLOWED_HOSTS=host.docker.internal` in `.env` so the registry accepts the fake servers. The auth server's MCP proxy applies its own guard as well, and on a local Docker Desktop stack it still blocks `host.docker.internal`, so whole-task calls do not reach the fake servers yet.

Three limits apply:

- The billing tasks depend on who asks, and the task text never says. Every task runs with one token, so without memory the agent can only guess between the two billing backends. The `groups` summary shows that gap.
- Skills are not scored. The eval agent has no tool to load a skill, so the runner checks tools only.
- The agent sends each search's results, with their input schemas, back to the model. Ten results per search fill much of an 8,192-token context, and when a conversation runs past it Ollama truncates the prompt, which only its own server log reports.

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
