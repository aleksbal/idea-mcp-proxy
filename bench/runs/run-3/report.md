# Benchmark results

Model `sonnet`, 3 rep(s), project Apache Commons Lang `rel/commons-lang-3.21.0`. Values are means over reps.

| task | case | arm | correct | cost $ | input tok | output tok | turns | IDE calls ok | built-in calls | wall s |
|---|---|---|---|---|---|---|---|---|---|---|
| change-signature | ide | grep | 3/3 | 0.087 | 70,408 | 3,937 | 16.0 | 0.0 | 15.0 | 38 |
| change-signature | ide | ide-proxy | 3/3 | 0.134 | 114,578 | 4,143 | 18.0 | 2.0 | 15.0 | 46 |
| grep-literal-files | grep | grep | 1/3 | 0.010 | 16,037 | 430 | 2.0 | 0.0 | 1.0 | 11 |
| grep-literal-files | grep | ide-proxy | 3/3 | 0.010 | 20,920 | 353 | 2.0 | 0.0 | 1.0 | 8 |
| grep-message-methods | grep | grep | 3/3 | 0.018 | 34,054 | 581 | 4.0 | 0.0 | 3.0 | 17 |
| grep-message-methods | grep | ide-proxy | 3/3 | 0.025 | 42,495 | 657 | 3.7 | 0.0 | 2.7 | 14 |
| impact-signature | ide | grep | 3/3 | 0.036 | 38,196 | 1,171 | 3.7 | 0.0 | 2.7 | 22 |
| impact-signature | ide | ide-proxy | 3/3 | 0.032 | 37,352 | 1,192 | 4.0 | 1.0 | 2.0 | 17 |
| overload-callers | ide | grep | 3/3 | 0.108 | 96,997 | 3,569 | 12.7 | 0.0 | 11.7 | 31 |
| overload-callers | ide | ide-proxy | 3/3 | 0.080 | 94,765 | 2,896 | 7.3 | 1.0 | 5.3 | 36 |
| rename-method | ide | grep | 3/3 | 0.104 | 110,226 | 4,774 | 25.7 | 0.0 | 24.7 | 45 |
| rename-method | ide | ide-proxy | 3/3 | 0.063 | 70,386 | 657 | 6.3 | 2.0 | 3.3 | 29 |
| transitive-callers | ide | grep | 2/3 | 0.079 | 119,792 | 2,704 | 10.7 | 0.0 | 9.7 | 36 |
| transitive-callers | ide | ide-proxy | 3/3 | 0.093 | 143,536 | 3,468 | 10.3 | 3.0 | 6.3 | 52 |
| zero-callers | ide | grep | 3/3 | 0.042 | 28,062 | 798 | 3.3 | 0.0 | 2.3 | 12 |
| zero-callers | ide | ide-proxy | 3/3 | 0.038 | 41,808 | 842 | 4.3 | 1.0 | 2.3 | 15 |

## Totals per case and arm (valid runs)

| case | arm | runs | correct | cost $ | mean turns | mean wall s | runs using the IDE |
|---|---|---|---|---|---|---|---|
| grep | grep | 6 | 4/6 | 0.08 | 3.0 | 14 | 0/6 |
| grep | ide-proxy | 6 | 6/6 | 0.10 | 2.8 | 11 | 0/6 |
| ide | grep | 18 | 17/18 | 1.37 | 12.0 | 31 | 0/18 |
| ide | ide-proxy | 18 | 18/18 | 1.32 | 8.4 | 32 | 18/18 |

## Incorrect answers

- `grep-literal-files__grep__1`: missing ['archutils'], extra -
- `grep-literal-files__grep__2`: missing ['archutils'], extra -
- `transitive-callers__grep__2`: missing ['eventbindinginvocationhandler.invoke'], extra ['proxyinvocationhandler.invoke']
