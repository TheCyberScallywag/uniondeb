"""The roster and generators produce valid, coherent events."""

import json
import random
from datetime import UTC, datetime

import pytest

from uniondeb.core.models import (
    ComputerDevice,
    DeviceType,
    HandoffStep,
    LandlineDevice,
    MobileDevice,
)
from uniondeb.synth.directory import Directory
from uniondeb.synth.registry import REGISTRY

NOW = datetime(2026, 11, 12, 14, 0, tzinfo=UTC)
MOB = MobileDevice(
    owner="Siân", imei="490154203237518", iccid="89014103211118510720", imsi="001010000000001", msisdn="+447700900001"
)
LAND = LandlineDevice(owner="Dafydd", number="+442920180001")
PC = ComputerDevice(owner="Rhys", ip="192.0.2.10")


@pytest.fixture
def directory() -> Directory:
    return Directory()


def test_directory_has_all_device_types(directory: Directory) -> None:
    assert all(directory.by_type[t] for t in DeviceType)
    # every number comes from an Ofcom drama range, so nothing can reach a real person
    assert all(d.msisdn.startswith("+447700900") for d in directory.by_type[DeviceType.MOBILE])
    assert all(d.number.startswith(("+442920180", "+441632960")) for d in directory.by_type[DeviceType.LANDLINE])


@pytest.mark.parametrize("key", list(REGISTRY))
def test_generators_produce_valid_round_trippable_events(key: str, directory: Directory) -> None:
    uc = REGISTRY[key]
    events = uc.generate(random.Random(7), directory, 200, NOW)
    assert events
    for ev in events:
        payload = json.loads(ev.model_dump_json())
        assert type(ev).model_validate(payload) == ev  # what the sink Cypher receives is lossless
        assert ev.kafka_key()


def test_handoff_steps_are_ordered_per_conversation(directory: Directory) -> None:
    steps = REGISTRY["handoff"].generate(random.Random(1), directory, 30, NOW)
    by_conv: dict[str, list[HandoffStep]] = {}
    for s in steps:
        assert isinstance(s, HandoffStep)
        by_conv.setdefault(s.conversation_id, []).append(s)
    for conv in by_conv.values():
        assert [s.seq for s in conv] == list(range(1, len(conv) + 1))
        assert [s.at for s in conv] == sorted(s.at for s in conv)
        assert len({s.device.owner for s in conv}) == 1  # one person hopping devices
        assert len({s.kafka_key() for s in conv}) == 1  # → same Kafka partition, in order


def test_cli_generate_and_stream(tmp_path, capsys) -> None:
    from uniondeb.core.cli import main as cli

    out = tmp_path / "run"
    assert cli(["synth", "generate", "--out", str(out), "--employees", "20", "--externals", "40", "--days", "3"]) == 0
    assert (out / "manifest.json").exists() and (out / "README.SYNTHETIC.txt").exists()
    capsys.readouterr()
    assert cli(["synth", "stream", "replay", "--run", str(out), "--speed", "0", "--limit", "5"]) == 0
    assert len(capsys.readouterr().out.strip().splitlines()) <= 5
