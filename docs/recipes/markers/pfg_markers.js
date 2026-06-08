JSON.stringify({
  pfgkAlert: document.querySelectorAll(".pfgkAlert").length,
  bookend:   document.querySelectorAll('[data-pfgk-role="bookend"]').length,
  seam:      document.querySelectorAll('[data-pfgk-role="seam"]').length,
  seamClean: document.querySelectorAll('[data-pfgk-role="seamClean"]').length,
  bridge:    document.querySelectorAll('[data-pfgk-role="bridge"]').length,
  broken:    document.querySelectorAll('[data-pfgk-role="broken"]').length,
  seamText:  (() => { const s = document.querySelector('[data-pfgk-role="seam"]'); return s ? s.textContent.replace(/\s+/g, " ").trim().slice(0, 400) : null; })(),
  dropClaim: document.body.textContent.includes("no persisted message is dropped")
})
