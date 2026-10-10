# Assets

`warmup.jpg` and `example.png` are `demo/demo_image1.jpg` and
`demo/demo_image2.png` from [rednote-hilab/dots.mocr](https://github.com/rednote-hilab/dots.mocr),
copyright (c) 2025 rednote-hilab, used under its MIT License.
`warmup.jpg` warms up vLLM before each GPU memory snapshot.

`screenshots/` holds README images made from saved dots.mocr responses for
public sample documents:

- [calibre's DOCX demo](https://calibre-ebook.com/downloads/demos/demo.docx),
  page 3, from the [calibre](https://calibre-ebook.com/) project.
- Pandoc's [equations example](https://github.com/jgm/pandoc/blob/main/wasm/examples/docx-with-equations-to-latex/equations.docx),
  from the [Pandoc](https://github.com/jgm/pandoc) repository.

The Word comparisons show the current exporter's output rendered by Microsoft
Word 16.

`benchmarks/` holds the graphs and raw per-request results of the 2026-10-10
benchmark described in the README's Performance section. Its pages came from
arXiv papers ([1706.03762](https://arxiv.org/abs/1706.03762),
[1810.04805](https://arxiv.org/abs/1810.04805),
[1512.03385](https://arxiv.org/abs/1512.03385),
[2010.11929](https://arxiv.org/abs/2010.11929),
[1409.0473](https://arxiv.org/abs/1409.0473),
[1502.03167](https://arxiv.org/abs/1502.03167)), IRS forms 1040, W-4,
Schedule C, Schedule 1, W-9, and 8949, and the Word exporter's regression pages.
The page images themselves are not included.
