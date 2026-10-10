# Project conventions

Follow the coding style of Tom Christie / Encode: small, explicit functions,
plain control flow, and abstractions that earn their place through actual use.

## Cleanup scope

Prefer removing unnecessary layers over adding configurable replacements. Keep
changes scoped to the requested cleanup, update affected imports and docs, and
run the relevant existing tests. Avoid compatibility shims for internal helpers
that have no supported callers.
