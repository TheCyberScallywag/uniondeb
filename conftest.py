"""Fixtures shared by every package: a synthetic run, loaded once into an embedded store.

Loading is the slow part, so it happens once per session into a Relate file; each test that wants
a store gets a private copy of that file and may write to it freely.
"""

import asyncio
import shutil
from pathlib import Path

import logfire
import pytest

from uniondeb.store import RelateRunner, StoreSettings, migrate
from uniondeb.store.load import load_run
from uniondeb.synth.generate import main as generate

SMALL = ["--days", "2", "--employees", "10", "--externals", "16", "--rows-per-file", "200", "--error-rate", "0.03"]
SCENARIO = ["--days", "14", "--employees", "30", "--externals", "60", "--rows-per-file", "400", "--error-rate", "0.01"]


def _build(tmp: Path, args: list[str]) -> tuple[Path, Path]:
    run, store = tmp / "run", tmp / "store.rl"
    generate(["--out", str(run), "--seed", "7", *args])

    async def load() -> None:
        runner = RelateRunner.open(str(store))
        try:
            await migrate(runner)
            await load_run(runner, run, StoreSettings(uri="relate::memory:", synthetic=True))
        finally:
            await runner.close()

    asyncio.run(load())
    return run, store


@pytest.fixture(scope="session", autouse=True)
def _quiet_logfire():
    logfire.configure(send_to_logfire=False, console=False)


@pytest.fixture(scope="session")
def small_run(tmp_path_factory) -> tuple[Path, Path]:
    """(run directory, loaded store file): two days, ten employees."""
    return _build(tmp_path_factory.mktemp("small"), SMALL)


@pytest.fixture(scope="session")
def scenario_run(tmp_path_factory) -> tuple[Path, Path]:
    """(run directory, loaded store file): fourteen days, both misconduct scenarios complete."""
    return _build(tmp_path_factory.mktemp("scenario"), SCENARIO)


async def _copy(store: Path, tmp_path: Path):
    target = tmp_path / "store.rl"
    shutil.copy(store, target)
    runner = RelateRunner.open(str(target))
    try:
        yield runner
    finally:
        await runner.close()


@pytest.fixture
async def small_store(small_run, tmp_path):
    async for runner in _copy(small_run[1], tmp_path):
        yield runner


@pytest.fixture
async def scenario_store(scenario_run, tmp_path):
    async for runner in _copy(scenario_run[1], tmp_path):
        yield runner


@pytest.fixture
async def empty_store():
    runner = RelateRunner.open(":memory:")
    await migrate(runner)
    yield runner
    await runner.close()


@pytest.fixture(scope="session")
def tagged_run(scenario_run, tmp_path_factory) -> tuple[Path, Path]:
    """(run directory, store file): the scenario run, tagged by Didolwr with a ground-truth judge."""
    from uniondeb.agents.didolwr import DidolwrConfig, EthicsJudge, tag_file
    from uniondeb.evals.scripted import ground_truth_judge, labels_by_text
    from uniondeb.extract import ContractRegistry, Echdynnwr
    from uniondeb.store import Tarddiad

    run, loaded = scenario_run
    tmp = tmp_path_factory.mktemp("tagged")
    store = tmp / "store.rl"
    shutil.copy(loaded, store)

    async def tag() -> None:
        runner = RelateRunner.open(str(store))
        try:
            t = Tarddiad(runner)
            echdynnwr = Echdynnwr(runner, t, ContractRegistry.load(), root=tmp / "extracts")
            judge = EthicsJudge(ground_truth_judge(labels_by_text(run)))
            config = DidolwrConfig(clean_sample_rate=1.0, question_review_rate=0.0, concurrency=8)
            for row in await runner.run("MATCH (f:SourceFile) RETURN f.sha256 AS sha ORDER BY sha"):
                await tag_file(runner, t, echdynnwr, row["sha"], judge=judge, config=config)
        finally:
            await runner.close()

    asyncio.run(tag())
    return run, store


@pytest.fixture
async def tagged_store(tagged_run, tmp_path):
    async for runner in _copy(tagged_run[1], tmp_path):
        yield runner
