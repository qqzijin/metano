/**
 * Knowledge-graph visual metadata shared between ForceGraph and its consumers
 * (labels/colors/widths are pure data, kept out of the component file so
 * react-refresh can fast-refresh the component without re-mounting it).
 */

export const ENTITY_TYPE_LABELS: Record<string, string> = {
  technology: "技术",
  concept: "概念",
  module: "模块",
  file: "文件",
};

export const REL_TYPE_LABELS: Record<string, string> = {
  related_to: "相关",
  imports: "导入",
  implements: "实现",
  implemented_by: "被实现",
  co_occurs_with: "共现",
};

const ENTITY_COLORS: Record<string, string> = {
  technology: "var(--chart-1)",
  concept: "var(--chart-2)",
  module: "var(--chart-4)",
  file: "var(--chart-3)",
};

const REL_COLORS: Record<string, string> = {
  imports: "var(--chart-3)",
  implements: "var(--chart-2)",
  implemented_by: "var(--chart-2)",
  related_to: "var(--muted-foreground)",
  co_occurs_with: "var(--chart-4)",
};

const REL_WIDTHS: Record<string, number> = {
  imports: 2,
  implements: 2,
  implemented_by: 2,
  related_to: 1.2,
  co_occurs_with: 1.5,
};

export function entityColor(type: string): string {
  return ENTITY_COLORS[type] ?? "var(--chart-5)";
}

export function relColor(type: string): string {
  return REL_COLORS[type] ?? "var(--muted-foreground)";
}

export function relWidth(type: string): number {
  return REL_WIDTHS[type] ?? 1.2;
}
