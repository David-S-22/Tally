"""Tests for the agentic loop's evidence collectors.

Only the deterministic parts are covered: compose parsing, repo-layout
derivation, and the workflow parser. Nothing here calls a model, opens a
socket or needs Docker -- the collectors are split from the loop precisely so
this is possible.
"""
import textwrap

import pytest

from sophia.agentic_loop import collect


# --- host port parsing -----------------------------------------------------

@pytest.mark.parametrize("entry, expected", [
    ("3005:80", 3005),                  # short form, the common case
    ("11434:11434", 11434),
    ({"published": 3005}, 3005),        # long form
    ({"published": "3005"}, 3005),      # long form, quoted in YAML
    ("127.0.0.1:3005:80", 3005),        # ip:host:container
    ("80", None),                       # container port only, host port random
    ({"target": 80}, None),             # long form with nothing published
    ("nonsense", None),
])
def test_host_port_forms(entry, expected):
    assert collect.host_port(entry) == expected


COMPOSE = textwrap.dedent("""
    services:
      bills-frontend:
        ports: ["3005:80"]
        depends_on: [bills-backend]
      bills-backend:
        ports: ["5005:5005"]
        depends_on:
          bills-db:
            condition: service_started
      bills-db:
        ports: [{published: 6005, target: 6005}]
      ollama:
        ports: ["127.0.0.1:11434:11434"]
      internal-only:
        image: busybox
""")


@pytest.fixture
def repo(tmp_path):
    """A miniature repository: compose file, student dirs, required dirs."""
    (tmp_path / "docker-compose.yml").write_text(COMPOSE, encoding="utf-8")
    for student in ("sophia", "aiden", "janelle"):
        (tmp_path / student / "backend").mkdir(parents=True)
    (tmp_path / "shared" / "backend").mkdir(parents=True)      # must be excluded
    (tmp_path / "student-3").mkdir()                           # no backend/, not a student dir
    (tmp_path / "docs").mkdir()
    (tmp_path / "ai-services").mkdir()
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    return tmp_path


def test_service_ports_reads_every_form(repo):
    compose = collect.load_compose(repo)
    assert collect.service_ports(compose) == {
        "bills-frontend": 3005,
        "bills-backend": 5005,
        "bills-db": 6005,
        "ollama": 11434,
        "internal-only": None,
    }


def test_services_with_suffix_filters_by_name(repo):
    compose = collect.load_compose(repo)
    assert collect.services_with_suffix(compose, "-db") == {"bills-db": 6005}
    assert set(collect.services_with_suffix(compose, "-backend")) == {"bills-backend"}


def test_dependency_edges_handles_list_and_mapping_forms(repo):
    compose = collect.load_compose(repo)
    assert collect.dependency_edges(compose) == [
        "bills-backend->bills-db",       # mapping form with a condition
        "bills-frontend->bills-backend",  # list form
    ]


def test_student_dirs_excludes_shared_and_dirs_without_a_backend(repo):
    assert collect.student_dirs(repo) == ["aiden", "janelle", "sophia"]


def test_load_compose_returns_none_when_absent(tmp_path):
    assert collect.load_compose(tmp_path) is None


# --- architecture mode -----------------------------------------------------

def test_collect_architecture_reports_inventory_edges_and_gaps(repo):
    ok, evidence = collect.collect_architecture(repo)
    assert ok
    assert "Compose defines 5 services" in evidence
    assert "bills-frontend:3005" in evidence
    assert "internal-only" in evidence
    assert "bills-frontend->bills-backend" in evidence
    assert "aiden, janelle, sophia" in evidence
    assert "shared" not in evidence.split("Student directories:")[1].split(".")[0]
    # scripts/ is absent from the fixture, so it must be reported missing
    assert "Missing required directories: scripts" in evidence
    assert "Shared frontend index.html present: False" in evidence


def test_collect_architecture_finds_the_shared_entry_point(repo):
    index = repo / "shared" / "frontend" / "public"
    index.mkdir(parents=True)
    (index / "index.html").write_text("<html></html>", encoding="utf-8")
    _, evidence = collect.collect_architecture(repo)
    assert "Shared frontend index.html present: True" in evidence


def test_modes_fail_cleanly_without_a_compose_file(tmp_path):
    for collector in (collect.collect_architecture, collect.collect_database,
                      collect.collect_endpoints):
        ok, message = collector(tmp_path)
        assert ok is False
        assert "docker-compose.yml" in message


def test_service_modes_need_matching_services(tmp_path):
    (tmp_path / "docker-compose.yml").write_text("services:\n  web:\n    image: nginx\n",
                                                 encoding="utf-8")
    ok, message = collect.collect_database(tmp_path)
    assert ok is False and "*-db" in message
    ok, message = collect.collect_endpoints(tmp_path)
    assert ok is False and "*-backend" in message


# --- devops mode -----------------------------------------------------------

WORKFLOW = textwrap.dedent("""
    name: Sophia-CI
    on:
      push:
        branches: [main]
        paths:
          - "sophia/**"
          - "shared/**"
    jobs:
      test:
        runs-on: ubuntu-latest
      docker-health:
        needs: test
        runs-on: ubuntu-latest
""")


@pytest.fixture
def no_gh(monkeypatch):
    """Keep the GitHub CLI out of the tests: no network, no subprocess."""
    monkeypatch.setattr(collect, "latest_run", lambda repo_root, workflow: None)


def test_workflow_student_is_derived_from_path_filters(repo):
    import yaml
    workflow = yaml.safe_load(WORKFLOW)
    assert collect.workflow_student(workflow, collect.student_dirs(repo)) == "sophia"


def test_workflow_student_reports_when_no_filter_matches(repo):
    import yaml
    workflow = yaml.safe_load("on:\n  push:\n    branches: [main]\njobs: {}\n")
    assert collect.workflow_student(workflow, collect.student_dirs(repo)) == \
        "no student path filter"


def test_collect_devops_lists_jobs_and_flags_the_naming_deviation(repo, no_gh):
    (repo / ".github" / "workflows" / "Sophia-CI.yml").write_text(WORKFLOW, encoding="utf-8")
    ok, evidence = collect.collect_devops(repo)
    assert ok
    assert "Sophia-CI.yml: covers [sophia]" in evidence
    assert "jobs [docker-health, test]" in evidence
    assert "NAMING DEVIATION" in evidence
    # three student dirs but only one workflow
    assert "Only 1 workflow file(s) for 3 student directories" in evidence


def test_collect_devops_survives_an_unparsable_workflow(repo, no_gh):
    (repo / ".github" / "workflows" / "broken.yml").write_text(
        "jobs: [unclosed\n", encoding="utf-8")
    ok, evidence = collect.collect_devops(repo)
    assert ok
    assert "broken.yml: could not be parsed" in evidence


def test_collect_devops_needs_a_workflows_directory(tmp_path):
    ok, message = collect.collect_devops(tmp_path)
    assert ok is False
    assert ".github/workflows/" in message
