# Ecosystem, Integration, and Interfaces

`weekly-scheduler` is the placement layer of the planning ecosystem.

## Boundary

```text
plan-editor -> plan -> time-boxer -> time representation
                                      |
                                      v
                              weekly-scheduler
                                      |
                                weekly schedule
                                      |
                                      v
                                   workflow
```

The scheduler does not need to know how a plan was produced. It receives explicit task/time contracts and places work within weekly constraints.

## Loose Coupling

The scheduler is coupled to contracts, not repositories.

It should consume:

- task identity;
- duration or 15-minute units;
- priority;
- dependencies;
- due dates;
- capacity constraints.

It should not depend on:

- the internal planning algorithm;
- a specific LLM;
- a specific UI;
- a specific calendar provider;
- workflow implementation details.

## Integration

The scheduler integrates components by translating planning information into placement decisions.

```text
planning semantics
       |
       | contract
       v
placement engine
       |
       | schedule contract
       v
execution layer
```

This distinction is important: **planning decides what should happen; scheduling decides when it can happen.**

## Interface

The primary scheduler interface is the weekly schedule.

A schedule should expose enough information for execution without exposing scheduler internals.

```yaml
schedule:
  week: 2026-W38
  items:
    - task: example
      start: "09:00"
      duration: 15m
```

The exact implementation can change while the contract remains stable.

## Ecosystem Responsibility

`weekly-scheduler` owns:

- weekly placement;
- capacity management;
- ordering;
- dependency-aware scheduling;
- schedule generation.

It does not own:

- task discovery;
- strategic planning;
- calendar UI;
- GitHub execution;
- retrospective interpretation.

## Calendar Boundary

A calendar is an external representation of scheduled time.

The scheduler should remain independent from any one calendar vendor.

```text
weekly schedule
      |
      v
calendar adapter
      |
      v
external calendar
```

This keeps the scheduling model portable.

## Design Principle

> **Plan semantically, schedule temporally, execute operationally.**

Each layer remains independently replaceable through explicit interfaces.
