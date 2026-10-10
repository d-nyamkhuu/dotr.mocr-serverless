import Markdown from "react-markdown";
import rehypeKatex from "rehype-katex";
import rehypeRaw from "rehype-raw";
import rehypeSanitize, { defaultSchema } from "rehype-sanitize";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import "katex/dist/katex.min.css";

// OCR Markdown mixes HTML tables with TeX. Sanitize the HTML before KaTeX
// runs, and drop images so the page never fetches URLs from model output.
const schema = {
  ...defaultSchema,
  tagNames: defaultSchema.tagNames?.filter((name) => name !== "img"),
};

export default function MarkdownPreview({ markdown }: { markdown: string }) {
  return (
    <div className="markdown-output">
      <Markdown
        remarkPlugins={[remarkGfm, remarkMath]}
        rehypePlugins={[rehypeRaw, [rehypeSanitize, schema], rehypeKatex]}
      >
        {markdown}
      </Markdown>
    </div>
  );
}
