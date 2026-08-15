/*
 * i1e: the chain-builder / render walker (Patches D + K). Wholesale readable
 * replacement of the vendor i1e; the apply step injects it after the $pfg block.
 *
 * It reparents the compaction working set, walks the lineage from the newest
 * turn back to the root, and decides how to render:
 *   - healthy chain -> normal render, plus a bookend at the root (green if the
 *     origin was reached, the loud failure marker if not);
 *   - corrupt chain (a cycle, or history stranded off the canonical walk) ->
 *     resplice every saved message in write order under a CHAIN CORRUPT banner,
 *     with per-message context banding, plus the same root verdict on top.
 *
 * THE FIX (docs/invariant.md): both root checks (the corrupt-path oldest-turn
 * check and the healthy-path terminus check) go through $pfg.classifyRoot, which
 * reads content, so a continuation preamble is a hard failure that fires the loud
 * marker, never a silent green "origin reconstructed" bookend.
 *
 * SSOT: every boundary/edge/origin/ghost decision I own or that is
 * behaviour-equivalent goes through $pfg. The two vendor selection heuristics
 * (the leaf-find collection `_leafCandidates` and the max-index pick `_pickNewest`) are preserved as-is.
 *
 * Vendor deps are referenced through the _pv_ namespace (see d1e.js): the driver
 * derives the set by grepping /_pv_\w+/ and binds each via a discovered alias
 * prologue. Each dep's identity lives once, in the anchor registry
 * (pfg/anchors.py), not restated here. NJ (the setImmediate yield-batch size) is
 * inlined below as the literal 8192 to shrink the surface by one name; a tuning
 * constant, not behaviour.
 */
async function i1e(_records, _telemetry) {
  let _byUuid = new Map();
  for (let _rec of _records) _byUuid.set(_rec.uuid, _rec);
  let _yieldCounter = 0;

  // ---- vendor: compaction reparent (osc relink). Logic preserved; the boundary
  // ---- test goes through $pfg. My addition: skip a boundary whose logical
  // ---- parent is already present (it is not a dangling reparent target).
  for (let _rec of _byUuid.values()) {
    if (!$pfg.isBoundary(_rec)) continue;
    if (_rec.logicalParentUuid && _byUuid.has(_rec.logicalParentUuid)) continue;
    let _preserved = _rec.compactMetadata?.preservedMessages,
      _segment = _rec.compactMetadata?.preservedSegment;
    if (_preserved) {
      if (_preserved.uuids.length === 0 || _preserved.uuids.some((_uuid) => !_byUuid.has(_uuid))) continue;
      let _parentCursor = _preserved.anchorUuid;
      for (let _uuid of _preserved.uuids) {
        let _preservedRec = _byUuid.get(_uuid);
        _byUuid.set(_uuid, { ..._preservedRec, parentUuid: _parentCursor });
        _parentCursor = _uuid;
      }
      let _headUuid = _preserved.uuids[0],
        _tailUuid = _preserved.uuids.at(-1);
      for (let [_uuid, _otherRec] of _byUuid) {
        if (++_yieldCounter % 8192 === 0) await new Promise((_resolve) => setImmediate(_resolve));
        if (_otherRec.parentUuid === _preserved.anchorUuid && _uuid !== _headUuid) _byUuid.set(_uuid, { ..._otherRec, parentUuid: _tailUuid });
      }
    } else if (_segment) {
      let _headRec = _byUuid.get(_segment.headUuid);
      // Guard like the preservedMessages path: if the segment's head or tail is not
      // present, skip. Otherwise the sweep below reparents records to an absent
      // tailUuid, creating a dangling parentUuid that dead-ends the canonical walk.
      if (!_headRec || !_byUuid.has(_segment.tailUuid)) continue;
      _byUuid.set(_segment.headUuid, { ..._headRec, parentUuid: _segment.anchorUuid });
      for (let [_uuid, _otherRec] of _byUuid) {
        if (++_yieldCounter % 8192 === 0) await new Promise((_resolve) => setImmediate(_resolve));
        if (_otherRec.parentUuid === _segment.anchorUuid && _uuid !== _segment.headUuid) _byUuid.set(_uuid, { ..._otherRec, parentUuid: _segment.tailUuid });
      }
    }
  }

  // vendor: last-occurrence index (the resplice below uses its own first-occurrence
  // index for chronological order; this one is not chronological).
  let _lastIdx = new Map();
  for (let _i = 0; _i < _records.length; _i++) _lastIdx.set(_records[_i].uuid, _i);

  // vendor: roots are records nobody parents; find the leaf turn under each. The
  // per-root walk follows $pfg.edge (my change: cross the logical parent too, so a
  // compaction does not hide the leaf). The user/assistant collection stays as the
  // vendor's loose leaf-find heuristic.
  let _parentedUuids = new Set();
  for (let _rec of _byUuid.values()) if (_rec.parentUuid) _parentedUuids.add(_rec.parentUuid);
  let _roots = [..._byUuid.values()].filter((_rec) => !_parentedUuids.has(_rec.uuid)),
    _leafCandidates = [];
  for (let _root of _roots) {
    let _rec = _root,
      _visited = new Set();
    while (_rec) {
      if (_visited.has(_rec.uuid)) break;
      _visited.add(_rec.uuid);
      if (_rec.type === "user" || _rec.type === "assistant") { _leafCandidates.push(_rec); break; }
      let _ref = $pfg.edge(_rec);
      _rec = _ref ? _byUuid.get(_ref) : void 0;
    }
  }
  if (_leafCandidates.length === 0) return [];

  // Prefer a real file-local leaf; fall back to any. `_mainLeaves`'s filter is exactly
  // $pfg.isMain (_leafCandidates is already user/assistant, so the remaining meta/sidechain
  // exclusion IS isMain; teamName is local ownership in teammate files). `_pickNewest` is
  // the vendor max-by-index pick, preserved.
  let _mainLeaves = _leafCandidates.filter((_rec) => $pfg.isMain(_rec)),
    _pickNewest = (_candidates) => _candidates.reduce((_newest, _cand) => ((_lastIdx.get(_cand.uuid) ?? -1) > (_lastIdx.get(_newest.uuid) ?? -1) ? _cand : _newest)),
    _leaf = _mainLeaves.length > 0 ? _pickNewest(_mainLeaves) : _pickNewest(_leafCandidates);

  // ---- guard #5: walk leaf -> root through the shared $pfg.walkToRoot. It carries
  // ---- the visited path, the crossed-lpu flag (bookend says "reconstructed"), the
  // ---- crossed-seam flag (K2 positional reattach -> root unverified), and a cycle
  // ---- reason, so this chain-builder no longer re-derives the walk inline. The same
  // ---- cycle guard kills the 4044ab66 lpu-into-preserved-tail and 9da63ece
  // ---- forward-edge cycles; on a cycle the terminus is the last distinct node
  // ---- reached, matching the old inline u[last].
  let _walk = $pfg.walkToRoot(_leaf.uuid, _byUuid),
    _renderOrder = _walk.path,
    _renderOrderUuids = new Set(_renderOrder.map((_rec) => _rec.uuid)),
    _term = _walk.terminus,
    // Policy folds over the walk's path (mechanism): recovered = crossed a compaction/fork
    // edge (bookend says "reconstructed"); crossedUnprovenSeam = a K2 seam on the path whose
    // same-conversation reattachment is not proven (root unverified). See pfg-core.
    _recovered = $pfg.crossedLpu(_walk.path),
    _crossedUnprovenSeam = $pfg.crossedUnprovenSeam(_walk.path),
    _cycleHit = _walk.reason === "cycle";

  // ---- outcome-gated corruption detection. _pfStranded: some unwalked main-line
  // ---- node has NO route to the canonical visited set _renderOrderUuids (a disjoint / dead-ended
  // ---- block). A healthy rewind/edit fork rejoins _renderOrderUuids at its fork point.
  let _walkMain = 0;
  for (let _rec of _renderOrder) if ($pfg.isMain(_rec)) _walkMain++;
  let _fileMain = 0;
  for (let _rec of _byUuid.values()) if ($pfg.isMain(_rec)) _fileMain++; // _byUuid is last-wins, so this is the UNIQUE count
  async function _pfStranded(_byUuid, _renderOrderUuids) {
    let _ops = 0,
      _joins = new Set(_renderOrderUuids);
    for (let _start of _byUuid.values()) {
      if (!$pfg.isMain(_start) || _renderOrderUuids.has(_start.uuid) || _joins.has(_start.uuid)) continue;
      let _cursor = _start,
        _seen = new Set(),
        _path = [],
        _reachedCanon = false;
      while (_cursor) {
        if (++_ops % 8192 === 0) await new Promise((_resolve) => setImmediate(_resolve));
        if (_joins.has(_cursor.uuid)) { _reachedCanon = true; break; } // reached the canonical chain (or a known-good node)
        if (_seen.has(_cursor.uuid)) break; // local cycle without joining -> stranded
        _seen.add(_cursor.uuid);
        _path.push(_cursor.uuid);
        let _ref = $pfg.edge(_cursor);
        if (!_ref) break; // reached a SEPARATE clean root -> disjoint tree
        _cursor = _byUuid.get(_ref);
      }
      if (_reachedCanon) { for (let _uuid of _path) _joins.add(_uuid); } // memoize the whole good path
      else return true; // a main-line turn with no path to canonical -> corrupt
    }
    return false;
  }
  let _corrupt = _cycleHit;
  if (!_corrupt && _walkMain < _fileMain) _corrupt = await _pfStranded(_byUuid, _renderOrderUuids);

  // Capture canonical reachability + terminus BEFORE the resplice reassigns _renderOrderUuids / nulls _term.
  let _term0 = _term,
    _canon = _renderOrderUuids,
    _pfBan = null;

  // THE root verdict, computed once and shared by both render paths. It is always
  // a statement about the LIVE leaf's lineage: did the walk from the newest turn
  // (_leaf) reach an origin by a VERIFIED path? Keyed on _term0 (the live leaf's
  // terminus), never on whichever fragment sorts oldest in the resplice: keying on
  // _renderOrder[0] there let a disjoint side-tree's origin suppress the marker while the live
  // lineage dead-ended. A crossed K2 seam is a positional reattachment whose root
  // is unproven, so it never counts as reaching the root. classifyRoot reads
  // content, so a continuation preamble is a failure here, not a success.
  let _rootCls = _term0 ? $pfg.classifyRoot(_term0) : { reachedRoot: false, reason: "none" };
  // An unresolved forkedFrom origin (source off-disk / copy absent from it) is a
  // dead end, not an origin: the first prompt lives in a source session we cannot
  // read, so completeness is undecidable. classifyRoot now OWNS this verdict
  // (reason "fork", reachedRoot false), parallel to the preamble dead end, so there
  // is no separate _forkUnresolved bolt-on here anymore. A soft "forked from..."
  // note would be a soft note for a hard failure (a forbidden middle); it is the
  // loud marker instead. A RESOLVED fork never lands on "fork": d1e either re-rooted
  // a mid-conversation branch (the terminus is the source's own origin) or flagged a
  // branch-at-source-start __pfgkForkComplete (classifyRoot -> origin -> green).
  // Divergent-lpu loud detect (never degrade silently). $pfg.byUuid is last-wins, so a
  // corrupt uuid-collision carrying TWO distinct logicalParentUuids would silently keep one
  // and could walk to a foreign origin: a false green. Structurally precluded in well-formed
  // data (a boundary's lpu is written once at compaction and copied verbatim on a fork, so it
  // cannot diverge; measured 0 across 146k uuids), but this complex exists FOR corrupted
  // lineage, so detect it over the assembled set and FAIL the trust verdict loud rather than
  // silently mis-pick. Detected here, not in d1e: i1e sees the identical assembled set in
  // _records and owns the verdict, so no cross-component flag is needed.
  let _divergentLpu = false;
  {
    let _lpuOf = new Map();
    for (let _rec of _records) {
      if (!_rec || !_rec.uuid || _rec.logicalParentUuid == null) continue;
      let _prev = _lpuOf.get(_rec.uuid);
      if (_prev === undefined) _lpuOf.set(_rec.uuid, _rec.logicalParentUuid);
      else if (_prev !== _rec.logicalParentUuid) { _divergentLpu = true; break; }
    }
  }
  let _reachedRoot = $pfg.rootTrusted(_walk) && !_divergentLpu;
  // The honest reason the live conversation's root is not present, shared by both
  // markers. `_where` names the terminus in that path's language.
  let _failDetail = (_where) =>
    _divergentLpu
      ? "A saved message appears more than once with DIFFERENT parent-compaction links (a corrupt uuid collision), so this conversation’s lineage cannot be trusted to a single root."
      : _rootCls.reason === "fork"
      ? "This conversation was branched (/fork) from a source session that is not saved in this folder, so the turns before the branch point, and the conversation’s first prompt, cannot be read from disk."
      : _crossedUnprovenSeam
      ? "This conversation’s compaction referenced a parent that was never saved to disk, so Patch K reattached the chain by position to the nearest preceding record. The origin shown is that record’s own; it cannot be confirmed as this conversation’s first prompt."
      : _rootCls.reason === "preamble"
      ? "The " + _where + " is a continuation summary (“This session is being continued…”), so the conversation’s first prompt predates the saved files."
      : "The walk from the latest turn reached " +
        (_term0 ? String(_term0.uuid).slice(0, 8) + " (" + _term0.type + ")" : "no record") +
        ", which is not a conversation origin, so the beginning of the conversation is not present.";

  if (_corrupt) {
    // ----- first-occurrence index = chronological order key (NOT vendor _lastIdx, which is
    //       last-occurrence). Computed up front: the resume-boundary pick keys on it. -----
    let _firstIdx = new Map();
    for (let _k = 0; _k < _records.length; _k++) if (!_firstIdx.has(_records[_k].uuid)) _firstIdx.set(_records[_k].uuid, _k);
    // ----- locate the boundary the MODEL resumes from, NOT the physically-last one. The
    //       physically-last compact_boundary can be a re-appended OLDER duplicate (measured
    //       on 9da: 586fd2dc re-appended after the real compaction c9a6635f), whose
    //       preservedMessages describe a different, older context; keying the in-context
    //       band on it makes "in resume context: N" confidently wrong (~118 vs the real 8).
    //       The boundary the model resumed from is the FIRST compaction reached walking UP
    //       from the live leaf (the leaf is its descendant), so take it off the live-leaf
    //       walk path; fall back to physical-last only if the walk reached no boundary. -----
    let _lastBoundary = null,
      _lastBoundaryIdx = -1;
    for (let _rec of _walk.path)
      if ($pfg.isBoundary(_rec)) { _lastBoundary = _rec; break; }
    if (!_lastBoundary)
      for (let _k = 0; _k < _records.length; _k++)
        if ($pfg.isBoundary(_records[_k])) _lastBoundary = _records[_k];
    if (_lastBoundary) _lastBoundaryIdx = _firstIdx.get(_lastBoundary.uuid) ?? -1;
    // _postReliable: the canonical walk reached/stopped at the boundary (or an
    // adjacent planted ghost), so _canon is COMPLETE for the post-boundary region
    // and we may band post-boundary reachability with confidence. If the walk died
    // mid-post-compaction, we cannot confirm membership and must hedge.
    let _postReliable =
      !!_term0 && ($pfg.isBoundary(_term0) || !!_term0.isCompactSummary || $pfg.isGhost(_term0));
    // ----- authoritative in-context membership: boundary union preservedMessages
    //       (PRIMARY), preservedSegment [head..tail] (SECONDARY), boundary-only
    //       best-effort (TERTIARY) -----
    let _live = new Set(),
      _bandMode = "bestEffort";
    if (_lastBoundary) {
      _live.add(_lastBoundary.uuid);
      let _preserved = _lastBoundary.compactMetadata?.preservedMessages,
        _segment = _lastBoundary.compactMetadata?.preservedSegment;
      if (_preserved && Array.isArray(_preserved.uuids)) {
        for (let _uuid of _preserved.uuids) _live.add(_uuid);
        _bandMode = "preserved";
      } else if (_segment && _segment.headUuid) {
        let _cursor = _byUuid.get(_segment.tailUuid) || _byUuid.get(_segment.headUuid),
          _visited = new Set();
        while (_cursor && !_visited.has(_cursor.uuid)) {
          _visited.add(_cursor.uuid);
          _live.add(_cursor.uuid);
          if (_cursor.uuid === _segment.headUuid) break;
          _cursor = _byUuid.get($pfg.edge(_cursor)); // follow the real edge (lpu too), not just parentUuid
        }
        _bandMode = "segment";
      }
    }
    // ----- per-record band. Post-boundary content the canonical walk did not reach
    //       is NEVER confident LIVE: "severed" (proven orphan) or "uncertain". -----
    let _liveCount = 0,
      _summarizedCount = 0,
      _severedCount = 0,
      _uncertainCount = 0,
      _liveUuids = [],
      _severedUuids = [],
      _uncertainUuids = []; // summarized is the implicit default; list only live/severed/uncertain
    for (let _rec of _byUuid.values()) {
      if (!$pfg.isMain(_rec) || $pfg.isGhost(_rec)) continue; // ghosts pass isMain; keep them out of the counts
      let _firstAppearance = _firstIdx.get(_rec.uuid) ?? -1,
        _band;
      if (!_lastBoundary) _band = "live"; // no compaction in file -> the whole chain is the context
      else if (_live.has(_rec.uuid)) _band = "live"; // authoritative: boundary union preserved
      else if (_firstAppearance > _lastBoundaryIdx) {
        // post-boundary by FIRST appearance (dup re-appends fail this)
        if (_canon.has(_rec.uuid)) _band = "live"; // proven leaf-ancestor -> in resume context
        // "severed" is a confident NOT-in-context claim, so it needs BOTH a clean walk to
        // the boundary (_postReliable) AND an authoritative live set (preserved/segment).
        // In bestEffort mode _live is only the boundary uuid and _canon follows the
        // max-index leaf, which can be the wrong branch in a multi-branch corpus; a
        // "severed" there would paint a confident red on maybe-live content, so we
        // downgrade to "uncertain" and let the banner's best-effort hedge stand.
        else if (_postReliable && _bandMode !== "bestEffort") _band = "severed"; // proven post-compaction orphan -> NOT in context
        else _band = "uncertain"; // walk unreliable or best-effort live set -> may-or-may-not be in context
      } else _band = "summarized"; // pre-boundary, non-preserved -> condensed into the summary
      _rec.__pfgkBand = _band;
      if (_band === "live") { _liveCount++; _liveUuids.push(_rec.uuid); }
      else if (_band === "summarized") _summarizedCount++;
      else if (_band === "severed") { _severedCount++; _severedUuids.push(_rec.uuid); }
      else { _uncertainCount++; _uncertainUuids.push(_rec.uuid); }
    }
    // ----- resplice spine: every unique renderable main-line turn, chronological
    //       (first-occurrence) order. Drop tool-result carriers (_pv_o1e reattaches
    //       them) and the planted pfgk- ghosts (a "crossed cleanly" card inside a
    //       damaged render is contradictory, and one of them caused the cycle).
    //       Content is last-wins because it comes from _byUuid. -----
    let _spine = [..._byUuid.values()].filter((_rec) => $pfg.isMain(_rec) && !_pv_s1e(_rec) && !$pfg.isGhost(_rec));
    _spine.sort((_recA, _recB) => (_firstIdx.get(_recA.uuid) ?? 2e9) - (_firstIdx.get(_recB.uuid) ?? 2e9));
    _renderOrder = _spine;
    _renderOrderUuids = new Set(_renderOrder.map((_rec) => _rec.uuid));
    _term = null; // replace broken walk; suppress healthy bookend/broken verdict
    _pfBan = {
      shown: _liveCount + _summarizedCount + _severedCount + _uncertainCount,
      live: _liveCount,
      sum: _summarizedCount,
      sev: _severedCount,
      unc: _uncertainCount,
      mode: _bandMode,
      cyc: _cycleHit,
      sid: _leaf.sessionId,
      leaf: _leaf.uuid,
      ts: (_lastBoundary && _lastBoundary.timestamp) || (_term0 && _term0.timestamp),
      lU: _liveUuids,
      sU: _severedUuids,
      uU: _uncertainUuids,
    };
  } else _renderOrder.reverse(); // healthy path only: leaf->root walk into chronological order

  await new Promise((_resolve) => setImmediate(_resolve));
  let _ren = _pv_o1e(_byUuid, _renderOrder, _renderOrderUuids);

  // The ONE canonical root-outcome failure marker (docs/invariant.md). Loud,
  // final, and honestly scoped: "not found in what we searched", never the
  // unproven universal "unrecoverable". Both failure paths route here so the
  // verdict renders identically, and it stays SEPARATE from the render-mode
  // CHAIN CORRUPT banner (two orthogonal facts, two strings).
  let _rootNotFound = ({ uuid, sessionId, timestamp, rows, detail, tm }) => ({
    type: "user",
    uuid,
    parentUuid: null,
    logicalParentUuid: null,
    sessionId,
    timestamp,
    message: {
      role: "user",
      content: $pfg.PFGK1_PREFIX + JSON.stringify({
        kind: "broken",
        badge: "⛔ TRANSCRIPT INCOMPLETE · Conversation root not found",
        glyph: "⛔",
        headline: "Conversation root not found in the saved files",
        rows,
        body:
          detail +
          " The backfill searched the other .jsonl files in this session’s own folder and did not find the root there. It may still exist in an archived or earlier session that was not searched.",
        tm: tm || "",
      }),
    },
  });

  // Wall-clock timing string, shown on EVERY terminal card (green bookend, the loud
  // root-not-found marker, and the CHAIN CORRUPT banner) so the reconstruction cost is
  // visible whatever the outcome, and arguably most useful on the failure paths
  // ("searched N files for X ms, still no root"). Display-only (invariant.md: Date.now
  // may feed timing telemetry, never the record set or order). Computed here, ABOVE the
  // corrupt/healthy fork, so both branches reference the one string.
  let _tmStr =
    _telemetry && _telemetry.timing
      ? "K stitching wall-clock: parse " + _telemetry.timing.parseMs + "ms · cross-file " + _telemetry.timing.crossFileMs + "ms · sibling " + _telemetry.timing.siblingBackfillMs + "ms · bookend " + _telemetry.timing.bookendMs + "ms"
      : "";

  if (Array.isArray(_ren) && _corrupt) {
    // ----- render-mode banner (orthogonal to the root verdict). No success language. -----
    let _rows = [
      ["messages shown", String(_pfBan.shown)],
      ["in resume context", String(_pfBan.live)],
      ["summarized away", String(_pfBan.sum)],
    ];
    if (_pfBan.sev > 0) _rows.push(["severed (not in context)", String(_pfBan.sev)]);
    if (_pfBan.unc > 0) _rows.push(["context uncertain", String(_pfBan.unc)]);
    _rows.push(["damage", _pfBan.cyc ? "cyclic chain" : "stranded history"]);
    let _body =
      "The saved conversation chain is corrupt" +
      (_pfBan.cyc
        ? " (a parent link loops back into already-seen messages)"
        : " (messages saved on disk are unreachable from the latest turn)") +
      ", so the normal lineage walk could not show the whole transcript. Every saved message is shown here in write order. Solid-gutter messages are in the model's context if you resume; dimmed messages were condensed into the compaction summary and are not in context." +
      (_pfBan.sev > 0 || _pfBan.unc > 0
        ? " Red/amber messages sit after the compaction but are not reachable as context."
        : "") +
      (_pfBan.mode !== "preserved"
        ? " (The in-context split is best-effort: this compaction has no preserved-message list.)"
        : "");
    _ren.unshift({
      type: "user",
      uuid: $pfg.GHOST_PREFIX + "resplice-" + _pfBan.leaf,
      parentUuid: null,
      logicalParentUuid: null,
      sessionId: _pfBan.sid,
      timestamp: _pfBan.ts,
      message: {
        role: "user",
        content: $pfg.PFGK1_PREFIX + JSON.stringify({
          kind: "resplice",
          badge: "⚠️ CHAIN CORRUPT · RESPLICED",
          glyph: "⚠️",
          headline: "Transcript respliced · chain corruption",
          rows: _rows,
          body: _body,
          bands: { live: _pfBan.lU, sev: _pfBan.sU, unc: _pfBan.uU },
          tm: _tmStr,
        }),
      },
    });
    // ----- HARD-FAILURE invariant. The verdict is about the LIVE leaf, not the
    //       oldest resplice turn: if the walk from the latest turn did not reach
    //       an origin by a verified path (_reachedRoot), fire the loud marker on
    //       top. Keying on _renderOrder[0] here let a disjoint side-tree's origin suppress the
    //       marker while the live lineage dead-ended (BREACH 1). -----
    if (!_reachedRoot) {
      _ren.unshift(
        _rootNotFound({
          uuid: $pfg.GHOST_PREFIX + "broken-" + (_term0 ? _term0.uuid : _leaf.uuid),
          sessionId: _pfBan.sid,
          timestamp: _pfBan.ts,
          rows: [
            ["latest turn", String(_leaf.uuid).slice(0, 8)],
            _crossedUnprovenSeam
              ? ["reattached by position", "root unverified"]
              : ["walk reached", _term0 ? String(_term0.uuid).slice(0, 8) + " (" + _term0.type + ")" : "none"],
            ["root reached", "NO"],
            // parity with the healthy-path marker: name the off-disk fork source here too.
            ...(_rootCls.reason === "fork" && _term0 && _term0.forkedFrom
              ? [["forked from", String(_term0.forkedFrom.sessionId).slice(0, 8) + " (off-disk)"]]
              : []),
          ],
          detail: _failDetail("oldest message the latest turn traces back to"),
          tm: _tmStr,
        }),
      );
    }
  } else if (Array.isArray(_ren) && _term) {
    // ----- healthy path. The root verdict (_reachedRoot) rests on in-band
    //       detection (classifyRoot reads the message text, since the vendor
    //       gives no out-of-band flag for a resume summary; guarded by
    //       isContinuationPreamble's string match) and is reattachment-aware: a
    //       preamble terminus or a crossed K2 seam is a hard failure (loud
    //       marker), NOT a green "reconstructed" bookend. -----
    let _root = _reachedRoot;
    if (!_root) {
      _ren.unshift(
        _rootNotFound({
          uuid: $pfg.GHOST_PREFIX + "broken-" + _term.uuid,
          sessionId: _term.sessionId,
          timestamp: _term.timestamp,
          rows: [
            ["walk terminus", String(_term.uuid).slice(0, 8) + " (" + _term.type + ")"],
            ["parentUuid", _term.parentUuid || "none"],
            ["lpu", _term.logicalParentUuid || "none"],
            ...(_crossedUnprovenSeam ? [["chain reattached", "by in-file position"]] : []),
            ["siblings examined", String(_telemetry ? _telemetry.siblingsScanned : 0)],
            ["phantoms backfilled", String(_telemetry ? _telemetry.phantomsBackfilled : 0)],
            ...(_rootCls.reason === "fork" ? [["forked from", String(_term.forkedFrom.sessionId).slice(0, 8) + " (off-disk)"]] : []),
          ],
          detail: _failDetail("deepest saved message"),
          tm: _tmStr,
        }),
      );
    } else if (_recovered) {
      // The origin, reached across a fork/compaction: the green bookend. Only
      // fires now when _root is true, so it can never sit on a preamble.
      let _xfSrc = _telemetry && _telemetry.provBasenames ? _telemetry.provBasenames[_term.uuid] : null;
      let _bkRows = _xfSrc
        ? [
            ["walk terminus", String(_term.uuid).slice(0, 8)],
            ["source sibling", _xfSrc],
            ...(_telemetry && _telemetry.prependedMessages > 0 ? [["msgs prepended", String(_telemetry.prependedMessages)]] : []),
            ...(_telemetry && _telemetry.phantomsBackfilled + _telemetry.phantomsCouldNotBackfill > 0
              ? [["phantoms backfilled", _telemetry.phantomsBackfilled + " of " + (_telemetry.phantomsBackfilled + _telemetry.phantomsCouldNotBackfill)]]
              : []),
          ]
        : [
            ["bridged across", "in-file compaction(s)"],
            ["walk terminus", String(_term.uuid).slice(0, 8)],
          ];
      let _bkHead = _xfSrc
        ? "Conversation origin · reconstructed from sibling fork"
        : "Conversation origin · reconstructed in-file";
      let _bkBody = _xfSrc
        ? "Patch K’s cross-conversation backfill found a sibling fork that retained the conversation’s pre-compaction content. The canonical chain root has been restored at the top of this transcript."
        : "The walk bridged back across one or more in-file compactions to a legitimate root, so the view is complete.";
      _ren.unshift({
        type: "user",
        uuid: $pfg.GHOST_PREFIX + "bookend-" + _term.uuid,
        parentUuid: null,
        logicalParentUuid: null,
        sessionId: _term.sessionId,
        timestamp: _term.timestamp,
        message: { role: "user", content: $pfg.PFGK1_PREFIX + JSON.stringify({ kind: "bookend", headline: _bkHead, rows: _bkRows, body: _bkBody, tm: _tmStr }) },
      });
    }
  }
  return _ren;
}
