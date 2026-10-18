# Inazuma Eleven GO Light / Shadow Randomizer

A standalone Python tool for randomizing an extracted `ie_a.fa` game archive. Includes a graphical interface, a command-line interface, reproducible seeds and detailed change logs.

This is the randomizer, separate from StatCalcIEGO1. Version 1.1, with English interface text, comments, help and logs.

## Requirements

- Python 3.8 or newer.
- Your own extracted `ie_a.fa` from Inazuma Eleven GO Light / Shadow.
- Tkinter for the graphical interface. It is usually included with Python on Windows and macOS. On Debian/Ubuntu, install it with `sudo apt install python3-tk`.

No third-party Python packages are required. Game files, ROMs and saves are not included. This program edits the archive; it does not extract a ROM or install a modified archive into a game.

## Graphical interface

Run:

```sh
python ie_randomizer.py
```

1. Browse to your original `ie_a.fa`.
2. Choose an output path. The default is a `randomized` folder beside the input archive.
3. Enter a seed, or leave it empty to generate one.
4. Choose the options and click **Randomize**.

**Dry run (log only)** previews the changes and writes a change log without modifying the game archive. The same input archive, seed and options reproduce the same randomized data.

## Command-line examples

Randomize using the default options and a fixed seed:

```sh
python ie_randomizer.py ie_a.fa --seed 1234
```

Enable character identity shuffling in addition to the defaults:

```sh
python ie_randomizer.py ie_a.fa --seed 1234 --characters shuffle
```

Shuffle character identities only, leaving the other randomization options off:

```sh
python ie_randomizer.py ie_a.fa --seed 1234 --characters shuffle --gender off --element off --position off --stats off --growth off --skills off --spirits off --equipment off --consumables off --prices off --shops off --spirit-stats off
```

Preview changes without modifying the archive:

```sh
python ie_randomizer.py ie_a.fa --seed 1234 --characters shuffle --dry-run
```

Choose an output path and the language of game names in the log:

```sh
python ie_randomizer.py ie_a.fa --seed 1234 --lang en -o randomized/ie_a.fa
```

See every option:

```sh
python ie_randomizer.py --help
```

## Randomization options

| Option | Modes | Default |
|---|---|---|
| `--characters` | `off`, `shuffle` | `off` |
| `--gender` | `off`, `random` | `random` |
| `--element` | `off`, `random` | `random` |
| `--position` | `off`, `random` | `off` |
| `--stats` | `off`, `shuffle`, `balanced`, `chaos` | `balanced` |
| `--growth` | `off`, `balanced`, `chaos` | `balanced` |
| `--skills` | `off`, `balanced`, `chaos` | `balanced` |
| `--spirits` | `off`, `random` | `random` |
| `--equipment` | `off`, `random` | `random` |
| `--consumables` | `off`, `shuffle` | `shuffle` |
| `--prices` | `off`, `random` | `random` |
| `--shops` | `off`, `random` | `random` |
| `--spirit-stats` | `off`, `shuffle` | `off` |

**Character identities** swaps names, models and related identity fields between non-placeholder character slots. Each slot retains its key, story references, stats, moves and growth data unless the corresponding independent randomization options modify those values. This is an identity swap, not a shuffle of script events. `--chars-keep-gender` restricts the identity shuffle to the same gender.

**Stats** can shuffle selected existing stats, redistribute a stat budget using vanilla position profiles, or sample values from vanilla stat distributions. **Growth** rearranges existing curve values or samples supported values, depending on the selected mode. These options modify game parameters; the program is not a level-stat calculator.

**Skills** reassign moves/passives from the game's eligible skill pool. **Fighting Spirits** are replaced only for players who already have one; their elements match the player by default. `--spirit-any-element` removes that restriction. `--female-chance` sets the gender probability from 0 to 1 (default: `0.5`).

## Output and logs

The default output is `randomized/ie_a.fa` beside the input archive. With a different output path, the program copies the input archive and patches the copy.

A UTF-8 change log is written beside the output as `ie_a.fa.spoiler_<seed>.txt`. During a dry run, the log is written beside the **input** archive. Use `--no-log` to disable it. Logs contain spoilers, including character replacements and move changes.

The interface and log labels are English. Game names default to English (`--lang en`); `es`, `de`, `fr` and `it` can also be selected from the command line. Names depend on the text tables present in the input archive, with an English fallback. This setting does not translate the game itself.

`--inplace` explicitly edits the original archive. It creates `<input>.bak` only if that backup does not already exist. Using the default separate output preserves the original input. `--dump-chars` prints raw character-base columns for inspecting the identity field mapping.

## Compatibility and verification

The tool expects the Light / Shadow ARC0 and CfgBin layouts and rejects unsupported table structures. It patches integer values without resizing the contained files. After writing, it checks table layouts and element/position values. These checks do not establish that every randomized combination is playable or that all story scenes display correctly.

For this English translation, three configurations were compared with the original Spanish version using the supplied GO Light archive: defaults, character identities restricted by gender, and unrestricted identities with chaos stats/growth/skills plus shuffled Fighting Spirit stats. All 75 modified/in-memory configuration files matched byte for byte in each comparison. English command-line help, a dry run and GUI labels were also checked. This was not an in-game playthrough or an independent test of Shadow.

## Repository files

- `ie_randomizer.py` — the complete application.
- `README.md` — these instructions.
- `.gitignore` — excludes game archives, backups, generated spoiler logs and Python caches.
# IEGO1Randomizer
