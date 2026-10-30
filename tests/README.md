# Tests

```powershell
.\.venv\Scripts\python.exe -m pytest                            # all tests
.\.venv\Scripts\python.exe -m pytest -m "not solidworks"        # unit layer, no SolidWorks, seconds
```

Integration tests (`solidworks` marker) drive a running SolidWorks and check each
result against a hand calculation; they skip when SolidWorks is not reachable.

## Writing SolidWorks tests

The integration layer takes about 15 minutes. What makes it slow is mostly the
number of documents it opens: each new part costs a few seconds, and SolidWorks
gets slower and uses more memory with every one (a run of ~1300 parts nearly
ran it out of memory). Before adding a test:

1. **Can it skip SolidWorks?** Geometry, parsing and planning logic belong in
   the unit layer (`test_logic.py`, `test_model_read.py`, ...). It runs in seconds.
2. **Extend a test before adding one.** If an existing test already builds the
   part you need, add your checks to it. Only a new kind of part needs a new test.
3. **Refusals go first, on the same part.** A refused call leaves the part as
   it was, so check a tool's errors before its passing case instead of in tests
   of their own. Make the refused call through `part.run_guarded(...)`, as the
   server does; unguarded, some tools leave a helper sketch or plane behind.
4. **Dimensions go with their feature.** Check that a returned dimension drives
   the geometry (`set_dimension`, then the volume) in the feature's own test.
5. **Several cases, one part.** Put variants side by side where they can't
   touch (three holes along a block, three pipes apart), and check each one's
   change in volume. Prefer a loop over `parametrize`, which builds a part per case.
6. **Build heavy setups once.** A part that takes many features to make (the
   drawing tests' plate, the joint linkage) is built and saved by a module- or
   session-scoped fixture, then opened by each test. In an assembly, move the
   components to the next arrangement instead of making a new assembly.
7. **Close what you open.** Use the `part` / `assembly` fixtures, or close in a
   `finally`. A test that leaves a document open fails, and conftest closes it.

To see what a change cost, time the layer before and after:
`pytest -m solidworks --durations=15`.

