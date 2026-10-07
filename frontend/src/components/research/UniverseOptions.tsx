import type { ResearchUniverseOption } from "@/lib/api";

export function UniverseOptions({ universes }: { universes: ResearchUniverseOption[] }) {
  const groups = universes.reduce<Array<{ label: string; items: ResearchUniverseOption[] }>>((result, item) => {
    const label = item.group_label || "其他股票池";
    const existing = result.find((group) => group.label === label);
    if (existing) existing.items.push(item);
    else result.push({ label, items: [item] });
    return result;
  }, []);

  return (
    <>
      {groups.map((group) => (
        <optgroup key={group.label} label={group.label}>
          {group.items.map((item) => (
            <option key={item.universe} value={item.universe}>
              {item.label} · {item.tier} · 成本{item.run_cost || "中"}
            </option>
          ))}
        </optgroup>
      ))}
    </>
  );
}
