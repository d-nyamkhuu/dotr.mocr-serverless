import { expect, test, type BrowserContext, type Page } from "@playwright/test";

const endpoint = "https://ocr.example.modal.run";
const token = "test-browser-token";
const markdown = `# Quarterly Research Notes

Q2 2024 | Applied AI Research Group
April 1, 2024

This quarter we explored the practical applications of large language models in document understanding. Our focus was on improving accuracy, reducing latency, and evaluating real-world use cases across different industries.

## Key Findings

- OCR accuracy improved by 12% compared to the previous quarter.
- Latency reduced from 4.8s to 3.1s on average.
- Strong performance on structured documents (invoices, reports).
- Identified edge cases involving low-contrast scans and rotated text.

## Benchmark Results

| Dataset           | Accuracy | Latency (s) | Pages |
| ----------------- | -------- | ----------- | ----- |
| Invoices          | 96.2%    | 2.9         | 500   |
| Research Papers   | 93.1%    | 3.4         | 200   |
| Handwritten Forms | 87.6%    | 4.8         | 100   |

## Next Steps

In Q3 we will expand our evaluation to multilingual documents, explore layout-aware models, and build a larger benchmark dataset to better represent real-world scenarios.`;
const result = {
  request_id: "607c2c28-1f3e-4b9a-b2d5-8e2f4c7a9d10",
  status: "ok",
  warnings: [],
  model: "dots-studio/dots.mocr",
  image: {
    original_width: 720,
    original_height: 1070,
    model_input_width: 728,
    model_input_height: 1064,
  },
  elements: [
    {
      bbox: [52, 69, 670, 133],
      category: "Title",
      text: "Quarterly Research Notes",
    },
    {
      bbox: [52, 228, 670, 359],
      category: "Text",
      text: "This quarter we explored the practical applications of large language models in document understanding.",
    },
    {
      bbox: [52, 384, 670, 560],
      category: "Section-header",
      text: "Key Findings",
    },
    {
      bbox: [52, 585, 670, 804],
      category: "Table",
      text: "<table><tr><td>Invoices</td><td>96.2%</td></tr></table>",
    },
  ],
  markdown,
  raw_text: '[{"category":"Title","text":"Quarterly Research Notes"}]',
  generation: {
    finish_reason: "stop",
    usage: { prompt_tokens: 1234, completion_tokens: 456, total_tokens: 1690 },
  },
};

async function documentImage(context: BrowserContext) {
  const page = await context.newPage();
  await page.setViewportSize({ width: 720, height: 1070 });
  await page.setContent(`<html><head><meta name="viewport" content="width=device-width, initial-scale=1"></head><body style="margin:0;background:white;color:#10182c;font:22px/1.4 'Times New Roman',serif;padding:68px 54px;box-sizing:border-box">
    <h1 style="font-size:43px;line-height:1.2;margin:0 0 16px;white-space:nowrap">Quarterly Research Notes</h1>
    <p style="font-size:19px;margin:0">Q2 2024 &nbsp; | &nbsp; Applied AI Research Group</p><p style="font-size:18px;color:#63769b;margin:6px 0 41px">April 1, 2024</p>
    <p style="margin:0 0 30px">This quarter we explored the practical applications of large language models in document understanding. Our focus was on improving accuracy, reducing latency, and evaluating real-world use cases across different industries.</p>
    <h2 style="font-size:28px;line-height:1.2;margin:0 0 9px">Key Findings</h2>
    <ul style="font-size:18px;line-height:1.7;padding-left:21px;margin:0 0 30px"><li>OCR accuracy improved by 12% compared to the previous quarter.</li><li>Latency reduced from 4.8s to 3.1s on average.</li><li>Strong performance on structured documents (invoices, reports).</li><li>Identified edge cases involving low-contrast scans and rotated text.</li></ul>
    <h2 style="font-size:28px;margin:0 0 10px">Benchmark Results</h2>
    <table style="border-collapse:collapse;width:100%;font-size:18px;margin-bottom:30px"><style>td,th{border:1px solid #d3dded;padding:9px 11px;text-align:left}th{background:#f3f6fa}</style><tr><th>Dataset</th><th>Accuracy</th><th>Latency (s)</th><th>Pages</th></tr><tr><td>Invoices</td><td>96.2%</td><td>2.9</td><td>500</td></tr><tr><td>Research Papers</td><td>93.1%</td><td>3.4</td><td>200</td></tr><tr><td>Handwritten Forms</td><td>87.6%</td><td>4.8</td><td>100</td></tr></table>
    <h2 style="font-size:28px;margin:0 0 10px">Next Steps</h2><p style="font-size:21px;margin:0">In Q3 we will expand our evaluation to multilingual documents, explore layout-aware models, and build a larger benchmark dataset to better represent real-world scenarios.</p>
    </body></html>`);
  const buffer = await page.screenshot({ scale: "css" });
  await page.close();
  return { name: "research-page.png", mimeType: "image/png", buffer };
}

async function connect(page: Page) {
  await page.getByRole("textbox", { name: "Endpoint URL" }).fill(endpoint);
  await page.getByLabel("Bearer token", { exact: true }).fill(token);
}

test.beforeEach(async ({ page }) => {
  await page.goto("/");
  await expect(page).toHaveTitle("OCR Studio");
  await expect(page.getByRole("heading", { name: "OCR Studio" })).toBeVisible();
});

test("upload, bearer auth, regions, output formats, and downloads", async ({
  page,
  context,
}, testInfo) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  let requests = 0;
  await page.route(`${endpoint}/v1/parse`, async (route) => {
    requests++;
    expect(route.request().headers().authorization).toBe(`Bearer ${token}`);
    expect(route.request().headers()["content-type"]).toContain(
      "multipart/form-data; boundary=",
    );
    expect(route.request().postDataBuffer()?.toString()).toContain(
      'filename="research-page.png"',
    );
    await route.fulfill({ json: result });
  });
  await connect(page);
  await page
    .getByLabel("Image file", { exact: true })
    .setInputFiles(await documentImage(context));
  await expect(
    page.getByAltText("Uploaded document with detected OCR regions"),
  ).toBeVisible();
  await page.getByRole("button", { name: "Run OCR", exact: true }).click();
  await expect(page.getByLabel("Response details")).toContainText("200 OK");
  await expect(page.getByLabel("Response details")).toContainText(
    "1,690 tokens",
  );
  await expect(page.getByRole("tabpanel")).toContainText(
    "# Quarterly Research Notes",
  );
  await page.getByRole("button", { name: "Zoom in", exact: true }).click();
  await expect(page.getByRole("button", { name: "Reset zoom" })).toHaveText(
    "125%",
  );
  await page.getByRole("button", { name: "Reset zoom" }).click();
  await page
    .getByRole("button", { name: "Region 1: Title", exact: true })
    .click();
  await expect(
    page.getByRole("tab", { name: "Regions", exact: true }),
  ).toHaveAttribute("aria-selected", "true");
  await expect(page.locator(".region-row.active")).toContainText(
    "Quarterly Research Notes",
  );
  await page.getByRole("tab", { name: "JSON", exact: true }).click();
  await expect(page.getByRole("tabpanel")).toContainText('"request_id"');
  const jsonDownload = page.waitForEvent("download");
  await page.getByRole("button", { name: "Download", exact: true }).click();
  expect((await jsonDownload).suggestedFilename()).toBe("research-page.json");
  await page.getByRole("tab", { name: "Raw output", exact: true }).click();
  await expect(page.getByRole("tabpanel")).toContainText(result.raw_text);
  await page.getByRole("tab", { name: "Markdown", exact: true }).click();
  const markdownDownload = page.waitForEvent("download");
  await page.getByRole("button", { name: "Download", exact: true }).click();
  expect((await markdownDownload).suggestedFilename()).toBe("research-page.md");
  await page.evaluate(async () => {
    await document.fonts.ready;
  });
  await page.screenshot({
    path: `/tmp/ocr-studio-${testInfo.project.name}.png`,
    fullPage: true,
  });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  expect(requests).toBe(1);
  expect(errors).toEqual([]);
});

test("connection survives reload, clears credentials, and masks the token", async ({
  page,
}) => {
  await connect(page);
  const secret = page.getByLabel("Bearer token", { exact: true });
  await expect(secret).toHaveAttribute("type", "password");
  await page.getByRole("button", { name: "Show token" }).click();
  await expect(secret).toHaveAttribute("type", "text");
  await page.reload();
  await expect(secret).toHaveValue(token);
  await expect(secret).toHaveAttribute("type", "password");
  await page.getByRole("button", { name: "Clear credentials" }).click();
  await page.reload();
  await expect(secret).toHaveValue("");
  await expect(page.getByRole("textbox", { name: "Endpoint URL" })).toHaveValue(
    endpoint,
  );
  expect(await page.evaluate(() => localStorage.length)).toBe(0);
});

test("URL requests and degraded responses preserve output without regions", async ({
  page,
}) => {
  await connect(page);
  await page.route(`${endpoint}/v1/parse`, async (route) => {
    expect(route.request().postData()).toContain(
      "https://images.example/page.png",
    );
    await route.fulfill({
      json: {
        ...result,
        status: "degraded",
        elements: null,
        warnings: ["Invalid layout JSON; returning raw text."],
      },
    });
  });
  await page.route("https://images.example/page.png", (route) => route.abort());
  await page.getByRole("button", { name: "Image URL", exact: true }).click();
  await page
    .getByRole("textbox", { name: "Image URL", exact: true })
    .fill("https://images.example/page.png");
  await page.getByRole("button", { name: "Run OCR", exact: true }).click();
  await expect(page.getByRole("status")).toContainText(
    "Recognition is degraded",
  );
  await expect(page.getByRole("tabpanel")).toContainText(
    "# Quarterly Research Notes",
  );
  await page.getByRole("tab", { name: "Regions", exact: true }).click();
  await expect(page.getByRole("tabpanel")).toContainText(
    "Layout regions are unavailable",
  );
});

test("invalid files, oversized images, and non-HTTPS endpoints are rejected locally", async ({
  page,
  context,
}) => {
  await connect(page);
  await page
    .getByLabel("Image file", { exact: true })
    .setInputFiles({
      name: "bad.pdf",
      mimeType: "application/pdf",
      buffer: Buffer.from("pdf"),
    });
  await expect(page.getByRole("alert")).toHaveText(
    "Choose a PNG or JPEG image.",
  );
  await page
    .getByLabel("Image file", { exact: true })
    .setInputFiles({
      name: "big.png",
      mimeType: "image/png",
      buffer: Buffer.alloc(20 * 1024 * 1024 + 1),
    });
  await expect(page.getByRole("alert")).toContainText("20 MiB");
  await page
    .getByLabel("Image file", { exact: true })
    .setInputFiles(await documentImage(context));
  await page
    .getByRole("textbox", { name: "Endpoint URL" })
    .fill("http://ocr.example.modal.run");
  await page.getByRole("button", { name: "Run OCR", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText(
    "Use an HTTPS endpoint URL",
  );
});

test("authentication and network failures are actionable without automatic retries", async ({
  page,
  context,
}) => {
  await connect(page);
  await page
    .getByLabel("Image file", { exact: true })
    .setInputFiles(await documentImage(context));
  let requests = 0;
  await page.route(`${endpoint}/v1/parse`, async (route) => {
    requests++;
    await route.fulfill({ status: 401, json: { detail: "Invalid token" } });
  });
  await page.getByRole("button", { name: "Run OCR", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText(
    "Authentication failed (HTTP 401)",
  );
  expect(requests).toBe(1);
  await page.unroute(`${endpoint}/v1/parse`);
  await page.route(`${endpoint}/v1/parse`, (route) => route.abort());
  await page.getByRole("button", { name: "Run OCR", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("network, and CORS");
});

test("stop waiting aborts locally and allows an explicit new request", async ({
  page,
  context,
}) => {
  await connect(page);
  await page
    .getByLabel("Image file", { exact: true })
    .setInputFiles(await documentImage(context));
  let release: () => void = () => {};
  await page.route(`${endpoint}/v1/parse`, async (route) => {
    await new Promise<void>((resolve) => {
      release = resolve;
    });
    await route.fulfill({ json: result }).catch(() => {});
  });
  await page.getByRole("button", { name: "Run OCR", exact: true }).click();
  await expect(page.getByRole("status")).toContainText(
    "Waiting for your Modal deployment",
  );
  await page.getByRole("button", { name: "Stop waiting", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText(
    "OCR may still be running on Modal",
  );
  await expect(
    page.getByRole("button", { name: "Run OCR", exact: true }),
  ).toBeEnabled();
  release();
});

test("malformed API responses do not crash the viewer or execute markup", async ({
  page,
  context,
}) => {
  await connect(page);
  await page
    .getByLabel("Image file", { exact: true })
    .setInputFiles(await documentImage(context));
  await page.route(`${endpoint}/v1/parse`, (route) =>
    route.fulfill({ json: { status: "ok", elements: [null] } }),
  );
  await page.getByRole("button", { name: "Run OCR", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("unexpected response");
  await page.unroute(`${endpoint}/v1/parse`);
  const markup = '<img src=x onerror="window.ocrUnsafe=true">';
  await page.route(`${endpoint}/v1/parse`, (route) =>
    route.fulfill({ json: { ...result, markdown: markup } }),
  );
  await page.getByRole("button", { name: "Run OCR", exact: true }).click();
  await expect(page.getByRole("tabpanel")).toHaveText(`1${markup}`);
  await expect(page.getByRole("tabpanel").locator("img")).toHaveCount(0);
});
