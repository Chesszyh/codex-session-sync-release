export async function fetchReplica(path, options = {}) {
  // Following an Access redirect would hide it behind a cross-origin fetch failure.
  const response = await fetch(path, { ...options, redirect: "manual" });
  if (response.type === "opaqueredirect" || response.status === 401 || response.status === 403)
    throw new Error("authentication_required");
  return response;
}
