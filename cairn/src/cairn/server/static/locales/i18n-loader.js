(function () {
  // Minimal runtime i18n loader. Strategy:
  // 1. Decide lang (localStorage.lang -> navigator.language -> 'en')
  // 2. Fetch /static/locales/{lang}.json
  // 3. Expose window.__LOCALE and window.t(key)
  // 4. Best-effort DOM replacement of exact-match text, placeholders, titles, and common elements
  // 5. Inject a small language selector into the header

  function detectLang() {
    const stored = localStorage.getItem('lang');
    if (stored) return stored;
    const nav = (navigator.language || navigator.userLanguage || 'en').toLowerCase();
    if (nav.startsWith('zh')) return 'zh-CN';
    return 'en';
  }

  async function loadLocale(lang) {
    try {
      const res = await fetch(`/static/locales/${lang}.json`, {cache: 'no-cache'});
      if (!res.ok) throw new Error('Locale not found');
      return await res.json();
    } catch (e) {
      // fallback to en
      if (lang !== 'en') return loadLocale('en');
      return {};
    }
  }

  function applyTranslations(map) {
    if (!map || typeof map !== 'object') return;

    // Replace whole-element textContent when it exactly equals a key
    const replaceTextNodes = () => {
      // Common elements to update
      const els = document.querySelectorAll('button, a, span, p, label, option, h1, h2, h3, h4, h5, h6, td, th');
      els.forEach(el => {
        const txt = el.textContent && el.textContent.trim();
        if (txt && map[txt]) {
          el.textContent = map[txt];
        }
      });
    };

    // Replace attributes: placeholder, title, aria-label, alt, value
    const replaceAttrs = () => {
      const attrs = ['placeholder', 'title', 'aria-label', 'alt', 'value'];
      attrs.forEach(attr => {
        const els = document.querySelectorAll('[' + attr + ']');
        els.forEach(el => {
          const v = el.getAttribute(attr);
          if (v && map[v]) el.setAttribute(attr, map[v]);
        });
      });
    };

    // Replace simple text nodes that equal keys (best-effort)
    const replaceTextNodesExact = () => {
      const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null, false);
      const toReplace = [];
      while (walker.nextNode()) {
        const node = walker.currentNode;
        const t = node.nodeValue && node.nodeValue.trim();
        if (t && map[t] && node.nodeValue.trim() === t) {
          toReplace.push({node, val: map[t]});
        }
      }
      toReplace.forEach(item => { item.node.nodeValue = item.val; });
    };

    replaceTextNodes();
    replaceAttrs();
    replaceTextNodesExact();
  }

  function injectSwitcher(currentLang) {
    try {
      const header = document.querySelector('header');
      if (!header) return;
      // Avoid duplicate
      if (document.getElementById('i18n-select')) return;

      const container = document.createElement('div');
      container.style.display = 'inline-flex';
      container.style.alignItems = 'center';
      container.style.gap = '6px';
      container.style.marginLeft = '8px';
      container.innerHTML = `
        <select id="i18n-select" aria-label="Language" class="h-7 rounded-md border px-2 text-xs">
          <option value="en">EN</option>
          <option value="zh-CN">中文</option>
        </select>
      `;
      header.appendChild(container);
      const sel = container.querySelector('#i18n-select');
      if (sel) {
        sel.value = currentLang;
        sel.addEventListener('change', (e) => {
          const v = e.target.value;
          localStorage.setItem('lang', v);
          // Reload to let heavy UI initialize with translations applied
          location.reload();
        });
      }
    } catch (e) { /* non-fatal */ }
  }

  // Public t() helper
  window.t = window.t || function (k) { return (window.__LOCALE && window.__LOCALE[k]) || k; };

  document.addEventListener('DOMContentLoaded', async function () {
    const lang = detectLang();
    const locale = await loadLocale(lang);
    window.__LOCALE = locale || {};
    // Expose t again with loaded locale
    window.t = function (k) { return (window.__LOCALE && window.__LOCALE[k]) || k; };

    try {
      applyTranslations(window.__LOCALE);
      injectSwitcher(lang);
      // If Alpine is present, provide a helper so developers can use t() in x-text or other bindings
      if (window.Alpine) {
        window.cairnTranslate = window.t;
      }
    } catch (e) {
      console.error('i18n-loader error', e);
    }
  });
})();
