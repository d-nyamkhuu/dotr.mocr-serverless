# dots.mocr Layout API on Modal

High-throughput, scale-to-zero document layout parsing with
[`dots-studio/dots.mocr`](https://huggingface.co/dots-studio/dots.mocr). The
public service is a small, protected API that accepts one PNG or JPEG and
returns everything needed to render the result: normalized layout elements,
Markdown, raw model output, dimensions, and token usage.

vLLM remains private inside each Modal GPU container. Concurrent API requests
are forwarded to its loopback-only OpenAI endpoint so vLLM can continuously
batch work on the GPU; the OpenAI-compatible routes are not publicly exposed.

[OCR Studio](playground/README.md) is a static frontend for testing a
deployment. It calls the API directly with your bearer token and displays image
regions, Markdown, raw output, and JSON.

Saved results can be turned into editable Word documents with the local
[Word exporter](#export-editable-word-documents).

## API

`POST /v1/parse` accepts `multipart/form-data` containing exactly one of:

- `file`: a PNG or JPEG upload, at most 20 MiB.
- `image_url`: a public HTTPS URL returning PNG or JPEG content.

The prompt, model, temperature, top-p, and output-token limit are controlled by
the service. There is no prompt ID, custom prompt, model selection, or
streaming mode.

Upload a local image:

```bash
curl -L "$DOTS_MOCR_URL/v1/parse" \
  -H "Authorization: Bearer $DOTS_MOCR_TOKEN" \
  -F "file=@assets/warmup.jpg;type=image/jpeg"
```

Parse a remote image:

```bash
curl -L "$DOTS_MOCR_URL/v1/parse" \
  -H "Authorization: Bearer $DOTS_MOCR_TOKEN" \
  -F "image_url=https://example.com/document.png"
```

The successful response has this shape:

```json
{
  "request_id": "607c2c28d14549c4a730e6e228df91e7",
  "status": "ok",
  "warnings": [],
  "model": "dots-studio/dots.mocr",
  "image": {
    "original_width": 1600,
    "original_height": 2200,
    "model_input_width": 1596,
    "model_input_height": 2184
  },
  "elements": [
    {
      "bbox": [120, 80, 1480, 190],
      "category": "Title",
      "text": "Example document"
    }
  ],
  "markdown": "Example document",
  "raw_text": "[...]",
  "generation": {
    "finish_reason": "stop",
    "usage": {
      "prompt_tokens": 1234,
      "completion_tokens": 456,
      "total_tokens": 1690
    }
  }
}
```

Bounding boxes use the EXIF-oriented original image coordinate space and
preserve the model's reading order. Tables remain HTML, formulas are normalized for
Markdown/LaTeX renderers. `Picture` elements retain their bounding boxes but
are omitted from `markdown`. Clients use the original image and returned
coordinates to draw overlays or crop pictures; the API returns no image data.
OCR Studio draws interactive regions over the source image in the browser.

The API returns token usage; the benchmark client measures request latency and
throughput. The API does not collect per-stage timings.

If the completion contains malformed layout JSON, the API returns a usable
HTTP 200 response with `status: "degraded"`, `elements: null`, raw text as Markdown,
the untouched model output, and a warning.
The parser expects the model's JSON array of layout elements; it does not repair
truncated JSON or accept alternate wrapper objects.

Expected errors use FastAPI's standard error response:

```json
{
  "detail": "Provide exactly one of file or image_url"
}
```

The protected deployment also exposes `/health`, `/metrics`, `/docs`, and
`/openapi.json`. Every route requires `Authorization: Bearer <token>`.
CORS permits requests from any origin with the `Authorization` and
`Content-Type` headers, without cookies. Preflight requests do not require
authentication. Enter the token at runtime in the tester; never bake it into
static assets. Modal proxy authentication is disabled on this OCR endpoint
so browser preflight reaches the application's CORS middleware.
Authentication runs inside the GPU container, so rejected requests can
still incur container startup compute even though they cannot run OCR.

## Deploy

Install [`uv`](https://docs.astral.sh/uv/getting-started/installation/), sync the
locked project environment, and select the intended Modal profile:

```bash
uv sync --locked
uv run modal profile current
```

Generate a random OCR bearer token:

```bash
uv run python -c 'import secrets; print(secrets.token_urlsafe(32))'
```

Put `DOTS_MOCR_TOKEN=<generated-token>` in a private, ignored `.env` file,
then create the OCR secret:

```bash
uv run modal secret create dots-mocr-api --from-dotenv .env
```

The OCR API refuses to start without this token. If workspace policy blocks
URLs without Modal proxy authentication, that policy must allow this
application-authenticated endpoint before deployment.

Download the exact model revision into the persistent model Volume, then
deploy:

```bash
uv run modal run modal_app.py::download_model
uv run modal deploy modal_app.py
```

The deploy command prints a URL similar to:

```text
https://<workspace>--dots-mocr-serverless-api.modal.run
```

The OCR API and bundled clients use `DOTS_MOCR_TOKEN` (or the clients' `--token`
option). The Modal CLI's `MODAL_TOKEN_ID`/`MODAL_TOKEN_SECRET` are separate
credentials. Never commit any of these secrets.

Deployment resources and scaling settings are declared directly on
`LayoutApiServer` in `modal_app.py`. Its snapshot hook starts and warms vLLM,
then puts it to sleep; the restore hook wakes vLLM before Modal serves the
FastAPI application directly through ASGI.

## Concurrent client and benchmark

The bundled client accepts any mixture of local PNG/JPEG paths and public HTTPS
URLs. It sends one image per request, preserves input ordering, and prints the
complete API responses:

```bash
export DOTS_MOCR_URL='https://<workspace>--dots-mocr-serverless-api.modal.run'
export DOTS_MOCR_TOKEN='YOUR_OCR_BEARER_TOKEN'

uv run python -m scripts.batch_client \
  --base-url "$DOTS_MOCR_URL" \
  --concurrency 32 \
  assets/warmup.jpg \
  assets/example.png
```

Run a concurrency sweep after deployment or after materially changing the
workload:

```bash
uv run python -m scripts.benchmark \
  --base-url "$DOTS_MOCR_URL" \
  --levels 8,16,32,64 \
  assets/warmup.jpg \
  assets/example.png
```

The report includes requests per second, completion tokens per second,
p50/p95 latency, and errors.

## Export editable Word documents

The optional Word exporter combines saved OCR results and their original PNG/JPEG
images into one `.docx`. Images and results must be supplied in the same order;
each image becomes a separate A4 page, using portrait or landscape orientation.
The exporter runs locally and does not call the OCR service or download models.

First, save the existing batch client's output:

```bash
uv run python -m scripts.batch_client \
  --base-url "$DOTS_MOCR_URL" \
  page1.png page2.jpg > results.json
```

After that command succeeds, export the document:

```bash
uv run --extra word python -m word_export \
  --results results.json \
  --output document.docx \
  page1.png page2.jpg
```

`--results` accepts the batch client's ordered result array, an ordered array of
raw `/v1/parse` responses, or one raw response for one image. Use the original
images corresponding to those results. The exporter checks result counts,
successful OCR status, bounding boxes, and EXIF-oriented dimensions before
writing output. Degraded results without layout elements cannot be exported.
Dimensions cannot detect an accidental substitution of a different same-sized
image, so preserve the input order yourself.

The document contains editable text, native Word tables (including merged
cells), and native Word equations. Markdown headings, emphasis, lists, and line
breaks are retained. Pictures are cropped from the source images. Page headers
and footers remain positioned content belonging to their respective page.
Source proportions are preserved when fitting each page onto A4; any remaining
space is centered around the source rectangle.

Unsupported equations remain editable LaTeX. Invalid or unsupported tables
remain editable HTML source. Warnings identify the page and region and are
written to stderr; successful exports return exit code zero even with warnings.
Invalid inputs or export failures return a nonzero exit code. The final file is
replaced atomically, so a failed export does not overwrite an existing document.

### Scanned and slightly rotated pages

For new scans, straighten the pixels **before OCR**, then use the same corrected
files for recognition and Word export:

```bash
uv run --extra word python -m scripts.prepare_scans \
  --output-dir straightened page1.png page2.jpg

uv run python -m scripts.batch_client \
  --base-url "$DOTS_MOCR_URL" \
  straightened/0001-page1.png straightened/0002-page2.png > results.json

uv run --extra word python -m word_export \
  --results results.json --output document.docx \
  straightened/0001-page1.png straightened/0002-page2.png
```

Set `DOTS_MOCR_URL` to the deployed API URL. The batch client uses
`DOTS_MOCR_TOKEN` as in the workflow above.
Preparation is local, keeps input order, applies EXIF orientation, and writes
PNG files plus an ordered `manifest.json` containing correction angles and
coordinate transforms. It requires a new output directory and preserves the
originals. `--angle -2` explicitly applies a clockwise two-degree correction
when automatic estimation is uncertain; supported corrections are within ±5°.

For **existing saved results**, supply the original matching images as usual.
The exporter automatically estimates small page skew from recognized text/table
regions, excluding pictures and explicitly rotated text. It rotates pixels and
bounding boxes together onto an expanded canvas, then removes blank corners and
neighboring text-line fragments from inflated text boxes. Recognized text and
table structure remain unchanged. Corrections produce review warnings; use
`--no-deskew` to retain the original coordinate system. Do not separately rotate
images belonging to saved OCR results without also transforming their boxes.

Detection uses consistent page rules or text baselines, with an approximately
0.15° deadband. Sparse, conflicting or out-of-range evidence is left unchanged;
uncertain candidate angles are reported. Preparation without OCR cannot exclude
photographs using layout labels, so inspect its manifest and corrected images.
Perspective distortion, curved pages, mixed tilts and OCR errors still need
review. Export-time correction cannot recover a failed OCR response: rerun OCR
on the prepared scan instead. Pale table rules softened by resampling get a
lower-contrast detection pass, still requiring the semantic boundary count.
Uncertain row measurements now warn instead of silently losing cell appearance.

### Conversion engine

Content conversion uses [Pandoc](https://pandoc.org/), installed with the pinned
`pypandoc-binary` package in the `word` extra. That package bundles Pandoc 3.9,
so exporting requires no separate Pandoc installation. Pandoc owns Markdown and
HTML parsing, table merges, list numbering, and LaTeX-to-Word equation conversion.
The repository's adapter validates OCR input, crops pictures, and uses
`python-docx` plus small OOXML helpers to position Pandoc's output in text boxes.
It does not implement another Markdown, table, or equation conversion engine.
OCR image markup is retained as alt text; only source-image crops are embedded.
Conversion does not fetch external resources. Pillow measures installed font
metrics, OpenCV measures source layout, and PDFium powers the optional rendered
checks. These maintained libraries are included in the `word` extra.

Other engines considered:

- [PaddleX's Word converter](https://github.com/PaddlePaddle/PaddleX/blob/develop/paddlex/inference/common/result/converter/word_converter.py)
  supports layout reconstruction, but the inspected implementation uses flowing
  paragraphs and does not preserve HTML row/column spans in its table parser.
- [Docling](https://docling-project.github.io/docling/concepts/serialization/)
  documents structured, Markdown, and HTML exports rather than a native Word
  reconstruction exporter.
- [pdf2docx](https://github.com/ArtifexSoftware/pdf2docx/discussions/374)
  targets PDF input and is no longer actively maintained by Artifex.

### Fidelity and verification

Microsoft Word desktop is the compatibility target. The current OCR schema
does not include fonts, line-level geometry, or exact table column widths.
The exporter defaults to Arial and prioritizes Latin/Cyrillic text. It measures
actual glyph advances, including bold and italic runs, and estimates font size
and line spacing from source-image text bands. Similar body-text sizes on a page
are kept consistent. Available whitespace below a region can accommodate font
leading. Font substitution, significant shrinking, and likely overflow produce
warnings; all recognized text remains in the document. Install Arial for the
closest agreement with the default measurements, or use `--font "Liberation Sans"`
with that same font installed on the machine rendering the document.

Table rules, column boundaries, row heights and solid cell fills are recovered
from source pixels when the measurements agree with the semantic table. Otherwise
Pandoc's widths are retained and uncertain columns are reported. Row heights are
minimums, allowing cells to grow; the containing text box can still overflow.
Merged cells remain native. Detection includes light rules on shaded headers,
merges adjacent antialiasing bands, and measures alignment over a cell's full
row span. This avoids mistaking header text for column boundaries.
Complex tables, especially several visual tables recognized as one table, still
need review. Generic borders are not added where none were detected.

For borderless tables, a complete set of header groups can be matched to gaps in
the body to recover unequal column widths. If pixel measurements remain
uncertain, columns use measured content proportions unless explicit HTML widths
are present. Dense tables use smaller cell margins and can fit down to 4 pt;
small text and remaining overflow produce warnings. The height estimate includes
the actual cell margins. Inconsistent OCR row widths are reported, but recognized
cells, values and row associations are not rewritten. Table fills require rule
evidence so photographed paper shadows do not become arbitrary gray cells.

Dark scan backgrounds use local contrast for text measurements. Small source
text can retain sizes below 6 pt, and short single-line estimates also respect
the available width. Low-contrast paper fragments are excluded from recovered
graphics. These measurements remain approximate; stamps and complex picture/text
overlaps still require review.

Balanced, attribute-free `<sup>`, `<sub>`, `<u>`, `<s>`, `<del>`, `<b>`, `<strong>`,
`<i>` and `<em>` tags become native formatting; unsupported markup stays literal.
Nested Markdown lists retain indentation. Regular explicit dotted hierarchies
such as `1.`, `1.1.`, `1.1.1.` become native multilevel numbering. Paragraph gaps
are estimated when the source image and multiple paragraphs provide evidence.
List indentation, tab stops and marker sizes are set before fitting; literal
Unicode bullets are consumed before native markers are generated. Single-line
font estimates account for whether the recognized letters have descenders.
Dominant text color is sampled from the region rather than inherited from the
Word heading style.
Scripted ordinary math parentheses are explicitly grouped for Pandoc/LibreOffice
compatibility. Existing sized delimiters and text commands are left intact;
failed equations retain their original LaTeX.
Expressions containing multiple roots carry a review warning: successful
conversion and matching glyphs cannot establish that OCR recognized root scope
correctly. The exporter preserves the recognized formula rather than guessing a
different mathematical expression.

Saved regions may optionally include `rotation`: `0`, `90`, `180`, or `270`
clockwise degrees. For narrow, tall text regions without rotation metadata, the
exporter compares recognized words with rotated glyphs and may infer a quarter
turn, or use an adjacent confidently oriented line. Inferences are warned;
ambiguous text stays horizontal. Dominantly Arabic/Hebrew paragraphs receive
bidirectional paragraph settings; mixed-script fidelity still needs review.

When a Picture overlaps a separately recognized text region on a clearly flat
background, the duplicate text pixels are replaced with that background before
the editable text is placed. Complex backgrounds remain untouched and
produce warnings. Large Picture regions also warn that any unrecognized text
or form fields inside remain raster content. Saved OCR alone cannot recover
editable fields that recognition omitted.

Additional substantial graphics are detected automatically. Existing Picture,
Table and Formula regions are excluded. Edge graphics can be cropped while
narrowing overlapping text boxes; candidates that would cover other editable
content are reported and omitted. Ambiguous residual ink is reported rather than
rasterized. Every recovered graphic carries a classification/overlap warning.
Use `--no-recover-graphics` to report candidates without embedding them. This is
conservative image analysis, not another OCR model, and can miss faint, small,
fragmented or overlapping graphics.

Editing text or substituting fonts can alter wrapping and create overlaps.
The output remains an editable approximation, not a pixel-identical reproduction.
No automated check can establish Microsoft Word rendering fidelity.

Run the integration tests against the bundled Pandoc engine:

```bash
uv run --extra word python -m unittest discover -s tests -v
```

The tests check content, equations, table merges, image crops, page sections,
coordinates, fallbacks, input errors, and atomic output. They do not run
Microsoft Word. Before relying on visual fidelity, open a representative export
in desktop Word, confirm it opens without a repair prompt, compare its page
count/order and region positions with the originals, and edit a paragraph,
merged table cell, and equation. Word desktop rendering is currently unverified.

### Rendered checks and repeatable regression comparisons

Render an export with local LibreOffice and produce an HTML report, page overlays,
region comparisons and JSON diagnostics:

```bash
uv run --extra word python -m scripts.check_word \
  --document document.docx --output word-checks page1.png page2.png
```

Alternatively export the DOCX to PDF in Microsoft Word and inspect that rendering:

```bash
uv run --extra word python -m scripts.check_word \
  --document document.docx --pdf document.pdf --renderer "Microsoft Word desktop" \
  --output word-checks --fail-on-warnings page1.png page2.png
```

`--pdf` must be a current rendering of the supplied DOCX. The report records both
file hashes but cannot prove that externally supplied PDFs correspond to a DOCX.
Checks report page-count/orientation mismatches, missing or clipped text,
characters outside text boxes, low ink overlap, and possible equation replacement
glyphs. Character recall ignores order and additional characters; PDF font
encodings can cause false positives. Ink F1 measures dark-pixel overlap with
3 pixels of tolerance at 150 DPI; it is not a percentage of fidelity. Low overlap
is flagged below `--min-ink-f1` (default 0.4). These are review aids, not proof of
correct equations or absence of clipping. Exit code 2 means findings when
`--fail-on-warnings` is set, 1 means a tool/input failure, and 0 means completion.

Replay the saved 11-page sample corpus without new inference requests:

```bash
uv run --extra word python -m scripts.word_roundtrip \
  --samples .cache/word-roundtrip-2026-10-03 \
  --output .cache/word-improved-2026-10-03
```

The default `all` phase exports, renders with local LibreOffice, and compares.
For an external renderer use `--phase export`, render the files in the output's
`reconstructed/` directory, then run `--phase check --renderer "renderer version"`.
Use `--pdf-directory` when those PDFs are elsewhere. Existing baseline files are
preserved; the output must be a separate directory. The command returns 2 for a
page-count regression, a drop greater than 1 percentage point in source-word
recall, or a drop greater than 3 points in mean ink F1. Mean scores can hide local
regressions, so inspect the linked page/region reports as well.

A corpus contains `selection.json` (ordered objects with `name` and `image`),
`ocr/NAME.json` (saved batch results), `originals/NAME.pdf` (reference rendering),
and optional `reconstructed/NAME.docx` plus `.pdf` (previous exporter baseline).
The downloaded samples and saved OCR are local artifacts, not bundled test data.
The source URLs and hashes are recorded in `tests/fixtures/word-roundtrip-sources.json`.

## Runtime and security

- vLLM `0.27.1`, Modal `1.5.4`, one L40S, 8 CPU cores, and 24 GiB host memory
  per replica.
- Model revision `e539fbb52280393adc081b289ec597430a0f9031` is stored under
  `/models/DotsMOCR-e539fbb5` in a persistent Modal Volume.
- Modal targets 32 concurrent inputs per replica and permits bursts to 64;
  additional demand scales horizontally up to a hard limit of two replicas.
- Replicas scale to zero after 30 idle seconds. No buffer replica is kept
  warm while the service is active.
- The Modal Function timeout and bundled client timeout are 15 minutes.
  The private inference request has a 14-minute timeout, leaving room for image
  transfer, processing, and returning the response. Container startup has a
  separate 30-minute timeout for model initialization and snapshot creation.
- Modal CPU/GPU memory snapshots capture a warmed vLLM process. vLLM is put
  into level-one sleep before capture and restored before the public API is
  marked ready, reducing repeated model initialization work after scale-to-zero.
- Snapshot warm-up uses the bundled `assets/warmup.jpg` document,
  the same image preprocessing as the parse API, and three 16-token completions.
- Image preparation uses Pillow's Lanczos resizing directly, retaining the
  previous 200/96 scale, pixel limits, and 28-pixel grid without converting
  through PDF. Resampling changes the pixels, so OCR output may differ.
  Preparation runs in Starlette's thread pool; inference uses asynchronous HTTP.
  Decoded original and model images are closed before inference starts. Only
  dimensions and the encoded model request are retained while awaiting vLLM.
- Remote images must use HTTPS. The service rejects private/reserved addresses,
  revalidates redirects, disables environment proxies, and limits download size
  and duration. Pillow checks the actual PNG/JPEG format when decoding.
  The address check happens before httpx connects, so it does not stop DNS
  rebinding; use network egress rules if strict isolation is required.
- Requests may exceed Modal's initial 150-second web interval during cold starts
  or long generations. Clients must follow redirects (`curl -L`; enabled in the
  bundled client).

## Development

```bash
uv sync --locked --extra word
uv run python -m unittest discover -s tests
```

- `layout_api.py`: the FastAPI service that runs inside each GPU container.
- `modal_app.py`: Modal images, model download, and the snapshot-aware server.
- `word_export/`: the Word exporter library (`python -m word_export`).
- `scripts/`: the batch client, benchmark, scan preparation, and Word checks.
- `playground/`: OCR Studio, the static test frontend.

## Model license and acceptable use

The model snapshot includes its own `dots.mocr LICENSE AGREEMENT` in addition
to this repository's MIT license. It permits API deployment but contains
responsible-use, privacy, copyright, security, and attribution obligations.
Review it before processing personal, regulated, or copyrighted documents.
