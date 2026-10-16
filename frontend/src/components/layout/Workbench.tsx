import { useEffect, useRef, type ReactNode } from "react";
import { ChevronDown } from "lucide-react";

export const WORKBENCH_PATHS = new Set([
  "/", "/premarket-news", "/premarket-news/list", "/single-stock-overnight",
  "/watchlist", "/trading-calendar", "/creator-opinions", "/portfolio", "/soxl-quant", "/batch-tasks",
]);

// Keep existing table contents and interactions; expose their real headers in the narrow layout.
export function WorkbenchSurface({ active, children }: { active: boolean; children: ReactNode }) {
  const root = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!active || !root.current) return;
    const labelTables = () => {
      root.current?.querySelectorAll("table").forEach(table => {
        const headers = Array.from(table.tHead?.rows[0]?.cells || []);
        if (!headers.length) return;
        table.classList.add("workbench-table");
        table.setAttribute("role", "table");
        table.querySelectorAll("tr").forEach(row => row.setAttribute("role", "row"));
        headers.forEach(header => header.setAttribute("role", "columnheader"));
        table.querySelectorAll("tbody tr").forEach(row => {
          if (row.closest("table") !== table) return;
          let column = 0;
          Array.from(row.children).forEach(cell => {
            if (!(cell instanceof HTMLTableCellElement)) return;
            cell.setAttribute("role", "cell");
            cell.dataset.label = headers[column]?.textContent?.trim() || "操作";
            cell.dataset.fullRow = String(cell.colSpan > 1);
            column += cell.colSpan;
          });
        });
      });
    };
    labelTables();
    const observer = new MutationObserver(labelTables);
    observer.observe(root.current, { childList: true, subtree: true, characterData: true });
    return () => observer.disconnect();
  }, [active]);
  return <div ref={root} className={active ? "workbench-surface h-full min-w-0" : "h-full"}>{children}</div>;
}

export function WorkbenchDisclosure({ title, children, defaultOpen = false, id }: {
  title: string; children: ReactNode; defaultOpen?: boolean; id?: string;
}) {
  return <details id={id} className="workbench-disclosure" open={defaultOpen || undefined}>
    <summary><span>{title}</span><ChevronDown className="h-4 w-4 shrink-0" aria-hidden="true" /></summary>
    <div className="workbench-disclosure-body">{children}</div>
  </details>;
}
