# Getting started

This starts the whole system on your machine, on the synthetic database.

## You need

- Docker with Compose
- [uv](https://docs.astral.sh/uv/) and Python 3.14, for the command-line tools and the delivery harness

## 1. Install the workspace

```sh
uv sync --all-packages
```

## 2. Configure the stack

```sh
cp deploy/.env.example deploy/.env
```

Edit `deploy/.env`:

1. Set `UNIONDEB_NEO4J_PASSWORD` (12 characters or more).
2. Read the Neo4j evaluation licence and set `NEO4J_ACCEPT_LICENSE_AGREEMENT=eval`.
3. Add the three secrets the API and worker need:

```sh
echo "UNIONDEB_FIELD_KEY=$(uv run uniondeb store keygen | tail -1)" >> deploy/.env
echo "UNIONDEB_PSEUDONYM_KEY=$(openssl rand -hex 32)"               >> deploy/.env
echo "UNIONDEB_AUTH_SECRET=$(openssl rand -hex 32)"                 >> deploy/.env
```

!!! warning "Keep the field key"
    `UNIONDEB_FIELD_KEY` encrypts record content in the database. If you lose it, that content
    cannot be read. `UNIONDEB_AUTH_SECRET` is for development sign-in only; production uses OIDC.

## 3. Start it

```sh
docker compose --profile app up -d --build --wait
```

| Service | What it does |
|---|---|
| `neo4j` | Neo4j Enterprise, standing in for AuraDB. Browser on <http://localhost:7474>, Bolt on 7687 |
| `neo4j-init` | One-shot: creates the `uniondeb-synthetic` database |
| `migrate` | One-shot: applies the migrations to both databases |
| `api` | The API, review console and portal on <http://localhost:8000> |
| `worker` | Runs queued and scheduled jobs |

The API and worker in this stack always work on `uniondeb-synthetic`.

Without `--profile app` only Neo4j and the migrations start, which is enough for the tests.

## 4. Use the command line from your machine

Tools you run on the host read their settings from the environment:

```sh
set -a; . deploy/.env; set +a
uv run uniondeb store status --synthetic
```

## 5. Sign in

Mint a development token for the role you want and paste it at <http://localhost:8000/login>:

```sh
uv run uniondeb api token --sub ann --roles reviewer
uv run uniondeb api token --sub lead --roles reviewer,compliance_lead
uv run uniondeb api token --sub olwen --roles nominated_officer
uv run uniondeb api token --sub ifan --roles investigator --person-id CS-0030
```

Tokens last one hour. An investigator's token needs `--person-id`, their own roster id: the
conflict-of-interest check refuses anyone it cannot place on the roster.

The same token works as a bearer token for the API:

```sh
TOKEN=$(uv run uniondeb api token --sub ann --roles reviewer)
curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/review/queues
```

## 6. Load data

Build the [test data](test-data.md), then send it [directly](run-direct.md) or through the
[delivery harness](run-harness.md).

## Stop it

```sh
docker compose --profile app down        # add -v to delete the database volumes too
```

## Without Docker

The API and worker also run against an embedded Relate file, which needs no server. It is meant
for tests and experiments, and for one process at a time:

```sh
export UNIONDEB_NEO4J_URI=relate:/tmp/uniondeb.rl
uv run uniondeb store migrate
```
