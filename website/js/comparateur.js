// Comparateur de grilles, partagé par les pages « Comparaison » des deux corpus.
//
// La page hôte déclare ce qu'elle compare avant de charger ce fichier :
//   window.DX_SOURCE   chemin du JSON de comparaison, depuis la racine du site
//                      (défaut : data/comparaisons.json)
//   window.DX_BASE     chemin de la racine du site depuis la page hôte (défaut : "").
//                      Les pages vivent dans website/pages/, d'où « ../ » : les chemins du
//                      JSON — sa propre URL et celles des aperçus — sont écrits depuis la
//                      racine, et c'est ce préfixe qui les y ramène.
//   window.DX_REBUILD  commande à afficher si le JSON est absent, puisqu'il n'est pas
//                      versionné et se régénère depuis S3.
// Tout le reste — statuts, appariement, survol lié — ne dépend que du contenu du JSON,
// dont la structure est la même pour les deux corpus (cf. website/scripts/build_data.py).
(function () {
  "use strict";

  var BASE = window.DX_BASE || "";
  var SOURCE = BASE + (window.DX_SOURCE || "data/comparaisons.json");
  var REBUILD = window.DX_REBUILD ||
    "uv run --project website python website/scripts/build_data.py";

  // Un chemin du JSON, ramené à la page courante.
  function chemin(relatif) {
    return BASE + relatif;
  }

  var STATUS_LABEL = {
    "ok": "valeur correcte",
    "format": "mêmes chiffres, format différent",
    "ocr": "erreur de lecture de chiffres",
    "deplacee": "valeur extraite, mais à une autre position",
    "manquante": "cellule vide, valeur introuvable",
    "differente": "valeur franchement différente",
    "non-appariee": "ligne ou colonne non appariée",
    "vide-attendue": "cellule vide dans l'annotation"
  };
  var STATUS_CLASS = {
    "ok": "dx-st-ok",
    "format": "dx-st-format",
    "ocr": "dx-st-ocr",
    "deplacee": "dx-st-deplacee",
    "manquante": "dx-st-manquante",
    "differente": "dx-st-differente",
    "non-appariee": "dx-st-nonappariee"
  };
  var COUNT_ORDER = [
    "ok", "format", "ocr", "deplacee", "manquante", "differente", "non-appariee"
  ];

  var app = document.getElementById("dx-app");
  var selTable = document.getElementById("dx-table");
  var selMethod = document.getElementById("dx-method");
  var selLayout = document.getElementById("dx-layout");
  var selFilter = document.getElementById("dx-filter");
  var DATA = null;

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = text;
    return n;
  }

  function pct(x) {
    return x === null || x === undefined ? "n/a" : (x * 100).toFixed(0) + " %";
  }

  // ── Sélecteurs ────────────────────────────────────────────────────────────

  function worstRate(t) {
    var rates = Object.keys(t.methods)
      .map(function (m) { return t.methods[m].recoveryRate; })
      .filter(function (r) { return r !== null && r !== undefined; });
    return rates.length ? Math.min.apply(null, rates) : null;
  }

  function passesFilter(t, mode) {
    var r = worstRate(t);
    if (mode === "bad") return r !== null && r < 0.5;
    if (mode === "zero") return r === 0;
    if (mode === "perfect") return r === 1;
    if (mode === "hdr") {
      return Object.keys(t.methods).some(function (m) {
        return !t.methods[m].headerRowsAgree;
      });
    }
    return true;
  }

  function fillTableSelect() {
    var mode = selFilter.value;
    var previous = selTable.value;
    selTable.innerHTML = "";
    var shown = DATA.tables.filter(function (t) { return passesFilter(t, mode); });
    if (!shown.length) {
      selTable.appendChild(el("option", null, "aucun tableau"));
      selTable.disabled = true;
      return;
    }
    selTable.disabled = false;
    shown.forEach(function (t) {
      var o = el("option");
      o.value = t.id;
      var r = worstRate(t);
      o.textContent = t.id + "  (" + t.annRows + "×" + t.annCols +
        (r === null ? "" : ", " + pct(r)) + ")";
      selTable.appendChild(o);
    });
    // On reste sur le tableau courant s'il survit au filtre.
    if (shown.some(function (t) { return t.id === previous; })) selTable.value = previous;
  }

  function fillMethodSelect(table) {
    var previous = selMethod.value;
    selMethod.innerHTML = "";
    var methods = DATA.meta.methods.filter(function (m) { return table.methods[m]; });
    if (methods.length > 1) {
      var both = el("option", null, "Les deux");
      both.value = "__both__";
      selMethod.appendChild(both);
    }
    methods.forEach(function (m) {
      var o = el("option", null, m);
      o.value = m;
      selMethod.appendChild(o);
    });
    var values = Array.prototype.map.call(selMethod.options, function (o) { return o.value; });
    selMethod.value = values.indexOf(previous) >= 0 ? previous : values[0];
  }

  // ── Rendu d'une grille ────────────────────────────────────────────────────

  function buildGrid(grid, opts) {
    // opts : {side, headerRows, headerCols, status, rowLink, colLink, nCols}
    // L'appariement n'est porté que par la grille annotée : c'est elle qui est la
    // référence, et c'est depuis elle que se lit ce qui a été retrouvé ou perdu.
    // La grille prédite reste neutre, pour que la couleur ne désigne qu'une chose.
    var wrap = el("div", "dx-grid-scroll");
    var table = el("table", "dx-grid");
    var nCols = opts.nCols;
    var rowLink = opts.rowLink || {};
    var colLink = opts.colLink || {};
    var tint = !!opts.rowLink;

    function linkOf(map, i) {
      if (!tint) return null;
      var v = map[String(i)];
      return v === undefined ? null : String(v);
    }

    function idxClass(to) {
      return "dx-idx" + (tint ? (to ? " dx-idx-ok" : " dx-idx-no") : "");
    }

    var colgroup = el("colgroup");
    colgroup.appendChild(el("col"));
    for (var c = 0; c < nCols; c++) {
      var col = el("col");
      col.dataset.col = String(c);
      colgroup.appendChild(col);
    }
    table.appendChild(colgroup);

    var thead = el("thead");
    var hrow = el("tr");
    var corner = el("th", "dx-idx dx-corner", "");
    corner.scope = "col";
    hrow.appendChild(corner);
    for (var c2 = 0; c2 < nCols; c2++) {
      var to = linkOf(colLink, c2);
      var th = el("th", idxClass(to), String(c2));
      th.scope = "col";
      if (tint) {
        th.title = to
          ? "colonne " + c2 + " appariée à la colonne " + to + " de la prédiction"
          : "colonne " + c2 + " non appariée";
      }
      hrow.appendChild(th);
    }
    thead.appendChild(hrow);
    table.appendChild(thead);

    var tbody = el("tbody");
    for (var r = 0; r < grid.length; r++) {
      var tr = el("tr");
      tr.dataset.row = String(r);
      var rowTo = linkOf(rowLink, r);
      var rh = el("th", idxClass(rowTo), String(r));
      rh.scope = "row";
      if (tint) {
        rh.title = rowTo
          ? "ligne " + r + " appariée à la ligne " + rowTo + " de la prédiction"
          : "ligne " + r + " non appariée";
      }
      tr.appendChild(rh);
      for (var c3 = 0; c3 < nCols; c3++) {
        var value = grid[r][c3] === undefined ? "" : grid[r][c3];
        var td = el("td", null, value);
        td.dataset.row = String(r);
        td.dataset.col = String(c3);
        td.dataset.side = opts.side;
        var inHdrRow = r < opts.headerRows;
        var inHdrCol = c3 < opts.headerCols;
        if (inHdrRow || inHdrCol) td.classList.add("dx-hdr");
        // Un libellé d'en-tête porte le sort de toute la ligne — ou de toute la
        // colonne — qu'il commande : appariée en vert, non appariée en rouge. Le
        // coin haut-gauche relève des deux axes à la fois et reste neutre.
        var role = null;
        var roleTo = null;
        if (tint && inHdrCol && !inHdrRow) { role = "row"; roleTo = rowTo; }
        else if (tint && inHdrRow && !inHdrCol) { role = "col"; roleTo = linkOf(colLink, c3); }
        if (role) {
          td.classList.add(roleTo ? "dx-hm-ok" : "dx-hm-no");
          td.dataset.hrole = role;
          if (roleTo) td.dataset.hlink = roleTo;
        }
        var status = opts.status ? opts.status[r + "," + c3] : null;
        if (status && STATUS_CLASS[status]) {
          td.classList.add(STATUS_CLASS[status]);
          td.dataset.status = status;
        }
        // La cellule appariée de l'autre grille, pour le survol lié.
        if (opts.side === "ann" && rowTo !== null) {
          var pc = linkOf(colLink, c3);
          if (pc !== null) {
            td.dataset.linkRow = rowTo;
            td.dataset.linkCol = pc;
          }
        }
        tr.appendChild(td);
      }
      tbody.appendChild(tr);
    }
    table.appendChild(tbody);
    wrap.appendChild(table);
    return wrap;
  }

  function maxCols(grid) {
    return grid.reduce(function (m, row) { return Math.max(m, row.length); }, 0);
  }

  function buildPane(title, swatchVar, dims, gridNode) {
    var pane = el("div", "dx-pane");
    var head = el("div", "dx-pane-head");
    var t = el("div", "dx-pane-title");
    if (swatchVar) {
      var sw = el("span", "dx-swatch");
      sw.style.background = swatchVar;
      t.appendChild(sw);
    }
    t.appendChild(el("span", null, title));
    head.appendChild(t);
    head.appendChild(el("div", "dx-pane-dims", dims));
    pane.appendChild(head);
    pane.appendChild(gridNode);
    return pane;
  }

  function summaryLine(result, nRows, nCols) {
    var box = el("div", "dx-summary");
    function item(label, value) {
      var s = el("span");
      s.appendChild(el("span", null, label + " "));
      s.appendChild(el("b", null, value));
      box.appendChild(s);
    }
    item("Valeurs attendues", String(result.expected));
    item("Récupérées", result.recovered + " (" + pct(result.recoveryRate) + ")");
    item("Lignes appariées", Object.keys(result.rowMatch || {}).length + " / " + nRows);
    item("Colonnes appariées", Object.keys(result.colMatch || {}).length + " / " + nCols);
    item("En-tête annotation", result.annHeaderRows + " ligne(s)");
    item("En-tête prédiction", result.predHeaderRows + " ligne(s)");
    var flag = el(
      "span",
      "dx-flag " + (result.headerRowsAgree ? "dx-flag-ok" : "dx-flag-warn"),
      result.headerRowsAgree ? "✓ hauteurs d'en-tête concordantes"
                             : "✗ hauteurs d'en-tête discordantes"
    );
    box.appendChild(flag);
    return box;
  }

  function countsLine(result) {
    var parts = COUNT_ORDER.filter(function (k) { return result.counts[k]; })
      .map(function (k) { return result.counts[k] + " " + STATUS_LABEL[k]; });
    if (!parts.length) return null;
    var p = el("p", "dx-fig-sub", parts.join("  ·  "));
    p.style.marginTop = "0.4rem";
    p.style.marginBottom = "0";
    return p;
  }

  // ── Rendu principal ───────────────────────────────────────────────────────

  function render() {
    var table = DATA.tables.find(function (t) { return t.id === selTable.value; });
    app.innerHTML = "";
    if (!table) {
      app.appendChild(el("p", "dx-nodata", "Aucun tableau sélectionné."));
      return;
    }
    var methods = selMethod.value === "__both__"
      ? DATA.meta.methods.filter(function (m) { return table.methods[m]; })
      : [selMethod.value];

    methods.forEach(function (method) {
      var result = table.methods[method];
      if (!result) return;

      // L'annotation n'est portée par la méthode que si elle diffère de celle du tableau :
      // marker voit le PDF entier et rend parfois d'un seul tenant un tableau coupé par un
      // saut de page, sa référence est alors regroupée d'autant.
      var ann = result.ann || table.ann;
      var annCols = maxCols(ann);

      var block = el("section", "dx-fig");
      block.appendChild(el("p", "dx-fig-title", table.id + " — " + method));
      block.appendChild(summaryLine(result, ann.length, annCols));
      var counts = countsLine(result);
      if (counts) block.appendChild(counts);

      var panes = el("div", "dx-panes" + (selLayout.value === "side" ? " dx-side-by-side" : ""));
      panes.appendChild(buildPane(
        "Annoté à la main",
        null,
        ann.length + " × " + annCols,
        buildGrid(ann, {
          side: "ann",
          headerRows: result.annHeaderRows,
          headerCols: result.annHeaderCols,
          status: result.status,
          rowLink: result.rowMatch,
          colLink: result.colMatch,
          nCols: annCols
        })
      ));
      var predCols = maxCols(result.pred);
      panes.appendChild(buildPane(
        "Extrait par " + method,
        "var(--dx-" + method + ")",
        result.pred.length + " × " + predCols,
        buildGrid(result.pred, {
          side: "pred",
          headerRows: result.predHeaderRows,
          headerCols: result.predHeaderCols,
          nCols: predCols
        })
      ));
      block.appendChild(panes);

      var info = el("div", "dx-cellinfo",
        "Survolez une cellule de l'annotation pour voir son statut et la cellule appariée.");
      block.appendChild(info);
      wireHover(panes, info, result);
      app.appendChild(block);
    });

    // Le document source est le même quel que soit le moteur : il vient après la boucle,
    // une seule fois, sous les grilles qu'il permet de vérifier.
    var apercus = buildApercus(table);
    if (apercus) app.appendChild(apercus);
  }

  // ── Document source ───────────────────────────────────────────────────────

  function buildApercus(table) {
    // `<details>` fermé par défaut : le navigateur ne télécharge l'image qu'à l'ouverture,
    // ce qui compte à 2 200 px de grand côté et une centaine de documents publiés.
    var images = table.apercus;
    if (!images || !images.length) return null;

    var box = el("details", "dx-apercu");
    var head = el("summary", "dx-apercu-summary");
    head.appendChild(el("span", null, "Document d'origine"));
    head.appendChild(el("span", "dx-apercu-count",
      images.length > 1 ? images.length + " pages" : "1 page"));
    box.appendChild(head);

    var body = el("div", "dx-apercu-body");
    images.forEach(function (src, i) {
      var figure = el("figure", "dx-apercu-fig");
      var img = el("img", "dx-apercu-img");
      img.src = chemin(src);
      img.loading = "lazy";
      img.decoding = "async";
      img.alt = images.length > 1
        ? "Scan du document " + table.id + ", page " + (i + 1)
        : "Scan du document " + table.id;
      figure.appendChild(img);

      var caption = el("figcaption", "dx-apercu-cap");
      var lien = el("a", null,
        (images.length > 1 ? "Page " + (i + 1) + " — " : "") + "ouvrir en pleine taille");
      lien.href = chemin(src);
      lien.target = "_blank";
      lien.rel = "noopener";
      caption.appendChild(lien);
      figure.appendChild(caption);
      body.appendChild(figure);
    });
    box.appendChild(body);

    var note = el("p", "dx-apercu-note",
      "Aperçu réduit à 2 200 pixels de grand côté, la résolution à laquelle le moteur lit " +
      "le document : c'est donc bien ce qu'il a vu.");
    box.appendChild(note);
    return box;
  }

  // ── Survol lié ────────────────────────────────────────────────────────────

  function wireHover(panes, info, result) {
    var idle = "Survolez une cellule de l'annotation pour voir son statut et la cellule appariée.";

    function clear() {
      panes.querySelectorAll(".dx-linked, .dx-focus").forEach(function (n) {
        n.classList.remove("dx-linked", "dx-focus");
      });
      panes.querySelectorAll("tr.dx-row-hl").forEach(function (n) {
        n.classList.remove("dx-row-hl");
      });
      panes.querySelectorAll("col.dx-col-hl").forEach(function (n) {
        n.classList.remove("dx-col-hl");
      });
      info.textContent = idle;
    }

    function describe(td) {
      var r = td.dataset.row, c = td.dataset.col;
      var status = td.dataset.status;
      var parts = ["ligne " + r + ", colonne " + c];
      var role = td.dataset.hrole;
      if (role) {
        var axis = role === "row" ? "ligne" : "colonne";
        parts.push("en-tête de " + axis + " : " + JSON.stringify(td.textContent));
        parts.push(td.dataset.hlink !== undefined
          ? "→ apparié à la " + axis + " " + td.dataset.hlink + " de la prédiction"
          : "→ non apparié : les valeurs de cette " + axis + " ne peuvent pas être comparées");
        info.textContent = parts.join("  ·  ");
        return;
      }
      if (td.dataset.side === "ann") {
        parts.push("attendu : " + JSON.stringify(td.textContent));
        if (td.dataset.linkRow !== undefined) {
          var pane = panes.children[1];
          var sel = 'td[data-row="' + td.dataset.linkRow + '"][data-col="' + td.dataset.linkCol + '"]';
          var target = pane ? pane.querySelector(sel) : null;
          parts.push("apparié à ligne " + td.dataset.linkRow + ", colonne " + td.dataset.linkCol);
          if (target) parts.push("prédit : " + JSON.stringify(target.textContent));
        } else if (status === "non-appariee") {
          parts.push("aucune position appariée");
        }
        if (status) parts.push("→ " + STATUS_LABEL[status]);
      } else {
        parts.push("prédit : " + JSON.stringify(td.textContent));
      }
      info.textContent = parts.join("  ·  ");
    }

    function highlight(td) {
      clear();
      td.classList.add("dx-focus");
      var tr = td.closest("tr");
      if (tr) tr.classList.add("dx-row-hl");
      var table = td.closest("table");
      var col = table ? table.querySelector('col[data-col="' + td.dataset.col + '"]') : null;
      if (col) col.classList.add("dx-col-hl");
      if (td.dataset.side === "ann" && td.dataset.linkRow !== undefined) {
        var pane = panes.children[1];
        var sel = 'td[data-row="' + td.dataset.linkRow + '"][data-col="' + td.dataset.linkCol + '"]';
        var target = pane ? pane.querySelector(sel) : null;
        if (target) target.classList.add("dx-linked");
      }
      // Sur un libellé d'en-tête, le lien porte sur toute la ligne ou toute la
      // colonne appariée — c'est ce qui rend le décalage de structure lisible.
      var facing = panes.children[1];
      if (facing && td.dataset.hrole && td.dataset.hlink !== undefined) {
        if (td.dataset.hrole === "row") {
          var otr = facing.querySelector('tr[data-row="' + td.dataset.hlink + '"]');
          if (otr) otr.classList.add("dx-row-hl");
        } else {
          var ocol = facing.querySelector('col[data-col="' + td.dataset.hlink + '"]');
          if (ocol) ocol.classList.add("dx-col-hl");
        }
      }
      describe(td);
    }

    panes.addEventListener("mouseover", function (ev) {
      var td = ev.target.closest("td");
      if (td && panes.contains(td)) highlight(td);
    });
    panes.addEventListener("mouseleave", clear);
    // Accès clavier : les cellules deviennent focusables à la demande.
    panes.querySelectorAll("td").forEach(function (td) {
      td.tabIndex = 0;
      td.addEventListener("focus", function () { highlight(td); });
    });
  }

  // ── Amorçage ──────────────────────────────────────────────────────────────

  function failure(message) {
    app.innerHTML = "";
    var box = el("div", "dx-nodata");
    box.appendChild(el("p", null, message));
    var p = el("p");
    p.style.marginBottom = "0";
    p.appendChild(el("span", null, "Régénérer les données : "));
    p.appendChild(el("code", null, REBUILD));
    box.appendChild(p);
    app.appendChild(box);
    [selTable, selMethod, selLayout, selFilter].forEach(function (s) { s.disabled = true; });
  }

  fetch(SOURCE)
    .then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    })
    .then(function (payload) {
      DATA = payload;
      fillTableSelect();
      var first = DATA.tables.find(function (t) { return t.id === selTable.value; });
      if (first) fillMethodSelect(first);
      render();

      selFilter.addEventListener("change", function () {
        fillTableSelect();
        var t = DATA.tables.find(function (x) { return x.id === selTable.value; });
        if (t) fillMethodSelect(t);
        render();
      });
      selTable.addEventListener("change", function () {
        var t = DATA.tables.find(function (x) { return x.id === selTable.value; });
        if (t) fillMethodSelect(t);
        render();
      });
      selMethod.addEventListener("change", render);
      selLayout.addEventListener("change", render);
    })
    .catch(function (err) {
      failure(
        "Les données de comparaison ne sont pas disponibles (" + err.message + "). " +
        "Le fichier " + SOURCE + " est produit depuis S3 et n'est pas versionné."
      );
    });
})();
