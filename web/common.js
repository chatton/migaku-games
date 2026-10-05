// Helpers shared by the pages (no build step: loaded with a plain <script>).
const $ = (id) => document.getElementById(id);

async function api(path, opts = {}) {
  const res = await fetch(path, opts);
  const body = res.headers.get("content-type")?.includes("json") ? await res.json() : null;
  if (!res.ok) throw new Error(body?.error || res.statusText);
  return body;
}

// Needs an element with id="toast" (styled by the page).
function toast(msg) {
  $("toast").textContent = msg;
  $("toast").classList.add("show");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => $("toast").classList.remove("show"), 2500);
}

// Migaku's toolbar sits over the top of the page. Measure how far down it reaches and expose
// that as --migaku-bar, re-measuring when it's injected (it renders a moment later in its
// shadow root) and on resize.
function followMigakuBar() {
  const measure = () => {
    const host = document.getElementById("MigakuShadowDom");
    let y = 0;
    while (host && y < 160 && document.elementFromPoint(innerWidth / 2, y + 1) === host) y += 2;
    document.documentElement.style.setProperty("--migaku-bar", y + "px");
  };
  const settle = () => [0, 500, 1500].forEach((ms) => setTimeout(measure, ms));
  new MutationObserver((records) => {
    if (records.some((r) => [...r.addedNodes].some((n) => n.id === "MigakuShadowDom"))) settle();
  }).observe(document.documentElement, { childList: true, subtree: true });
  addEventListener("resize", measure);
  settle();
}
