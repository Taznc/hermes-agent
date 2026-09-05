# Provider quota backoff

When a worker exits after logging `provider quota exhausted (429); retry after Ns`, the dispatcher treats that as an infrastructure interruption, not a task failure.

With `kanban.provider_backoff: true` (the default), Hermes records a durable provider pause in `kanban_provider_backoff` until the advertised deadline. The affected task is parked in `scheduled`; it is resumed to `ready` by the next dispatcher tick after the deadline. A task explicitly pinned to the same provider is guarded from dispatch during that interval, while tasks pinned to other providers continue normally. This state is stored in the board database and therefore survives a dispatcher restart.

Provider identity is taken from a task `provider_override`, or from the assignee profile's pinned `agent.provider`. Auto-routed profiles are not paused because their next run may resolve to a healthy provider.

Set `kanban.provider_backoff: false` to retain immediate re-dispatch after a quota interruption. The task still remains an `interrupted` infra event and does not consume its failure budget.

`hermes kanban stats` lists active pauses. `hermes kanban diagnostics` reports them when there are no task-specific diagnostics.
