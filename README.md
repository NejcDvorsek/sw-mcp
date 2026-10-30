# sw-mcp

[![PyPI](https://img.shields.io/pypi/v/sw-mcp)](https://pypi.org/project/sw-mcp/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](https://github.com/NejcDvorsek/sw-mcp/blob/main/LICENSE)

Let an AI agent (Claude, or any other MCP client) design parts, assemblies and
drawings in **your own SolidWorks**, and check its own work as it goes. After
every step it measures the result, so it can compare it with what you asked for
and fix mistakes instead of guessing.

sw-mcp is a fork of [SolidWorks-MCP](https://github.com/hjbaard/SolidWorks-MCP)
by Henk Jan Baard and the SolidWorks connector of
[Jig](https://github.com/NejcDvorsek/jig). It works with any MCP client.

<!-- mcp-name: io.github.NejcDvorsek/sw-mcp -->

## What it can do

- **Parts:** sketches, extrudes, revolves, sweeps and lofts; holes, threads,
  slots and pockets on any face; fillets, chamfers, shells, ribs, patterns and
  mirrors. Every sketch is fully defined, so the part stays editable.
- **Checking:** each step reports volume, mass and size; dimensions and mates
  are measured back. A step that can't be done fails with a clear reason
  instead of producing wrong geometry. `read_model` reads a whole part for a
  review in one call: holes with their sizes, internal radii, sharp inside
  corners, setups, thin walls and draft.
- **Sheet metal:** base flanges (flat tabs or bent L, U and Z sections) and
  edge flanges with your thickness, bend radius and K-factor; the flat blank's
  size, and the flat pattern with bend lines exported to DXF for laser
  cutting. Closed corners, ready to weld.
- **Welds and weldments:** weld beads between bodies, full or intermittent;
  frames of library tube, pipe, angle and channel profiles with mitred or
  butted corners, end caps and gussets; the cut list with each member's length
  and the total weld length.
- **Assemblies:** insert and place components, mate them, swap parts, check
  interference and step joints through their range of motion.
- **Drawings:** ISO sheets with a title block, views, sections and details,
  dimensions, hole callouts, surface finish and GD&T symbols, tables, and
  export to PDF, DXF or DWG. Sheet metal parts get their flat pattern with
  bend lines, bend notes and the blank's size; welded parts their weld symbols
  and cut list.
- **Your own parts:** open existing parts and STEP files, change dimensions
  and equations, set materials, and build on sketches you drew.
- **3D printing and export:** STEP, STL and 3MF export, screenshots,
  printability checks, and comparing a part with a scanned mesh.

## Get started

1. Install [uv](https://docs.astral.sh/uv/getting-started/installation/).
2. Start SolidWorks and leave it open.
3. Add the server to your MCP client. In Claude Code:

   ```bash
   claude mcp add solidworks -- uvx sw-mcp
   ```

   <details>
   <summary>Claude Desktop or another client</summary>

   ```json
   {
     "mcpServers": {
       "solidworks": {
         "command": "uvx",
         "args": ["sw-mcp"]
       }
     }
   }
   ```

   </details>

   With Jig there is nothing to add: the plugin starts sw-mcp itself.

4. Ask for a part:

   > Design a 100 × 80 × 8 mm mounting plate with a Ø16 mm centre bore, four
   > counterbored M5 holes 12 mm from the corners and R5 corners. Then export
   > an STL.

   Or a sheet metal one:

   > Make a 2 mm steel tray, 80 × 50 mm, with 20 mm flanges on the long sides
   > and an inner bend radius of 3 mm. Give me the flat pattern as a DXF and a
   > drawing as a PDF.

   Or a welded frame:

   > Weld a 600 × 400 mm frame of 30 × 30 × 2.6 square tube with mitred
   > corners, cap the open ends, and give me the cut list and a drawing.

## Requirements

- Windows with SolidWorks installed and **running**. The server connects to
  the open SolidWorks; it doesn't start it.
- Tested on SOLIDWORKS 2026. 2020 and newer should work but are untested.
- To check your setup, run this with SolidWorks open. It builds a few small
  test parts and closes them without saving:

  ```bash
  uvx sw-mcp --selftest
  ```

## Good to know

- **Early version (0.x):** it works end to end, but tool names may still change.
  See the [changelog](https://github.com/NejcDvorsek/sw-mcp/blob/main/CHANGELOG.md).
- **Local:** it talks only to the SolidWorks on your computer and makes no
  network calls.
- **Scripts are on by default:** `run_script` runs any Python it's given on
  your computer. Start the server with `--no-scripts` to turn it off.
- **Multibody parts:** face and edge tools only see the first body;
  `read_model` reads them all.

## Help and feedback

Something not working? Open a
[bug report](https://github.com/NejcDvorsek/sw-mcp/issues/new?template=bug_report.yml)
with the output of `uvx sw-mcp --selftest`. Running a SolidWorks version other
than 2026? A
[compatibility report](https://github.com/NejcDvorsek/sw-mcp/issues/new?template=compatibility.yml)
helps, whether it worked or not.

## Credits and license

MIT, see [LICENSE](https://github.com/NejcDvorsek/sw-mcp/blob/main/LICENSE).
The original server is by Henk Jan Baard
([hjbaard/SolidWorks-MCP](https://github.com/hjbaard/SolidWorks-MCP)); this
fork is maintained by Nejc Dvorsek.
