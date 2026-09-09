# Solve 4 statements — audit of the supplied wheels

Audited on 2026-09-05 without modifying archives or installed libraries. The audit covered both wheel ZIPs, the statement template, package validation, data preparation, and compilation commands. Installed files were compared byte-for-byte with their archive counterparts.

| Wheel | SHA-256 |
| --- | --- |
| solve_cli-1.0.14-py3-none-any.whl | `0d951711f82b0b10e1e676712697f7f57003882bdce06eac93de4783fd77c484` |
| libsolve-1.0.11-py3-none-any.whl | `94faa78ee06f8801734bdc1425c139d7f07438112c8f2a7242c31e8677c6a6e9` |

## Local behavior

`libsolve/package/package.py` validates that `description/<language>.md` (or a PDF) exists and that the default-language title exists. It does not validate Markdown syntax, section order, explanations, images, or `.example` filters. Native validation therefore does not guarantee statement rendering.

`Package.prepare()` compiles programs, generates inputs, and—except for interactive tasks—answers. It may accept an external `description_compile` callback. The supplied wheels do not include a Pandoc compiler or Solve filters.

`Test.output` uses the manifest output name; if absent, it changes the input extension from `.in` to `.out`. Imported names must therefore be retained.

## `description-compile`

The Solve CLI reads the description directory and `config.json`, runs `Package.prepare()`, and adds manifest test inputs and outputs separately only when each file is at most **2048 bytes**. Interactive test data is skipped. It uploads materials for PDF and HTML generation, then writes results as `description/<language>.pdf` and `.html`.

Images must be real files directly in `description/`, and example filters must point to real manifest-registered test filenames. The CLI does not search image subdirectories.

## Verification boundary

The supplied template confirms this filter form:

````markdown
``` {.example input_file="task0.in" output_file="task0.out"}
```
````

The packer preview is auxiliary: it expands example data and preserves explanations and images, but it is not a Solve compiler. The exported `en.md` retains `.example` filters; data is not copied into a separate Tests section.

The packer checks the 2048-byte limit (UTF-8, including a final newline for new samples), imported sample sizes, and required files during local package validation.
