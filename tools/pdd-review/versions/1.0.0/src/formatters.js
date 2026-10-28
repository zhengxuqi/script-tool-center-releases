export function productModel(specs) {
  if (specs == null) return "";
  let value = specs;
  if (typeof value === "string") {
    const trimmed = value.trim();
    if (!trimmed) return "";
    try {
      value = JSON.parse(trimmed);
    } catch {
      return trimmed;
    }
  }
  const entries = Array.isArray(value) ? value : [value];
  const objects = entries.filter((item) => item && typeof item === "object");
  const model = objects.find((item) => String(item.spec_key ?? item.key ?? item.name ?? "").includes("型号"));
  if (model) return String(model.spec_value ?? model.value ?? model.text ?? "");
  const fallback = objects.map((item) => item.spec_value ?? item.value ?? item.text).filter(Boolean);
  if (fallback.length) return fallback.map(String).join(" / ");
  return entries.filter((item) => ["string", "number"].includes(typeof item)).map(String).join(" / ");
}
