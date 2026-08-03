import React from "react";

function inline(text, keyPrefix = "t") {
  const parts = [];
  const re = /(\*\*[^*]+\*\*|\*[^*]+\*|`[^`]+`|\[[^\]]+\]\([^)]+\))/g;
  let last = 0;
  let m;
  let i = 0;
  while ((m = re.exec(text))) {
    if (m.index > last) parts.push(text.slice(last, m.index));
    const tok = m[0];
    const key = `${keyPrefix}-${i++}`;
    if (tok.startsWith("**")) {
      parts.push(<strong key={key}>{tok.slice(2, -2)}</strong>);
    } else if (tok.startsWith("`")) {
      parts.push(<code key={key}>{tok.slice(1, -1)}</code>);
    } else if (tok.startsWith("[")) {
      const mm = tok.match(/^\[([^\]]+)\]\(([^)]+)\)$/);
      parts.push(
        <a key={key} href={mm[2]} target="_blank" rel="noreferrer">
          {mm[1]}
        </a>
      );
    } else if (tok.startsWith("*")) {
      parts.push(<em key={key}>{tok.slice(1, -1)}</em>);
    }
    last = m.index + tok.length;
  }
  if (last < text.length) parts.push(text.slice(last));
  return parts;
}

export default function MarkdownView({ text }) {
  if (!text) return <div className="md-empty">暂无报告内容</div>;
  const lines = String(text).split(/\r?\n/);
  const blocks = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (/^```/.test(line)) {
      const buf = [];
      i += 1;
      while (i < lines.length && !/^```/.test(lines[i])) {
        buf.push(lines[i]);
        i += 1;
      }
      i += 1;
      blocks.push({ type: "code", text: buf.join("\n") });
      continue;
    }
    if (/^\|/.test(line)) {
      const tbl = [];
      while (i < lines.length && /^\|/.test(lines[i])) {
        tbl.push(lines[i]);
        i += 1;
      }
      blocks.push({ type: "table", rows: tbl });
      continue;
    }
    const h = line.match(/^(#{1,6})\s+(.*)$/);
    if (h) {
      blocks.push({ type: `h${h[1].length}`, text: h[2] });
      i += 1;
      continue;
    }
    if (/^\s*[-*]\s+/.test(line)) {
      const items = [];
      while (i < lines.length && /^\s*[-*]\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^\s*[-*]\s+/, ""));
        i += 1;
      }
      blocks.push({ type: "ul", items });
      continue;
    }
    if (/^\s*\d+[.)]\s+/.test(line)) {
      const items = [];
      while (i < lines.length && /^\s*\d+[.)]\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^\s*\d+[.)]\s+/, ""));
        i += 1;
      }
      blocks.push({ type: "ol", items });
      continue;
    }
    if (/^>\s?/.test(line)) {
      const buf = [];
      while (i < lines.length && /^>\s?/.test(lines[i])) {
        buf.push(lines[i].replace(/^>\s?/, ""));
        i += 1;
      }
      blocks.push({ type: "quote", text: buf.join(" ") });
      continue;
    }
    if (/^\s*(---+|\*\*\*+)\s*$/.test(line)) {
      blocks.push({ type: "hr" });
      i += 1;
      continue;
    }
    if (line.trim() === "") {
      i += 1;
      continue;
    }
    blocks.push({ type: "p", text: line });
    i += 1;
  }

  return (
    <div className="md">
      {blocks.map((b, bi) => {
        const key = `b-${bi}`;
        switch (b.type) {
          case "h1":
            return <h1 key={key}>{inline(b.text, key)}</h1>;
          case "h2":
            return <h2 key={key}>{inline(b.text, key)}</h2>;
          case "h3":
            return <h3 key={key}>{inline(b.text, key)}</h3>;
          case "h4":
          case "h5":
          case "h6":
            return <h4 key={key}>{inline(b.text, key)}</h4>;
          case "p":
            return <p key={key}>{inline(b.text, key)}</p>;
          case "ul":
            return (
              <ul key={key}>
                {b.items.map((it, ii) => (
                  <li key={ii}>{inline(it, `${key}-${ii}`)}</li>
                ))}
              </ul>
            );
          case "ol":
            return (
              <ol key={key}>
                {b.items.map((it, ii) => (
                  <li key={ii}>{inline(it, `${key}-${ii}`)}</li>
                ))}
              </ol>
            );
          case "quote":
            return <blockquote key={key}>{inline(b.text, key)}</blockquote>;
          case "code":
            return (
              <pre key={key}>
                <code>{b.text}</code>
              </pre>
            );
          case "hr":
            return <hr key={key} />;
          case "table": {
            const rows = b.rows.map((r) => r.split("|").slice(1, -1).map((c) => c.trim()));
            const hasSep = rows.length > 1 && /^[\s\-:|]+$/.test(rows[1].join("|"));
            const head = hasSep ? rows[0] : null;
            const body = hasSep ? rows.slice(2) : rows.slice(1);
            return (
              <table key={key}>
                {head ? (
                  <thead>
                    <tr>
                      {head.map((c, ci) => (
                        <th key={ci}>{inline(c, `${key}-h${ci}`)}</th>
                      ))}
                    </tr>
                  </thead>
                ) : null}
                <tbody>
                  {body.map((r, ri) => (
                    <tr key={ri}>
                      {r.map((c, ci) => (
                        <td key={ci}>{inline(c, `${key}-${ri}-${ci}`)}</td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            );
          }
          default:
            return null;
        }
      })}
    </div>
  );
}
