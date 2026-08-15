/*
 * d1e: the session loader (Patches J + K). Replaces the vendor loader body
 * wholesale. Readable source; the apply step injects it into the bundle after
 * the $pfg block (src/pfg-core.js via util/pfg-codegen.py).
 *
 * It parses the session .jsonl, then, before handing the records to the
 * chain-builder, recovers any lineage a compaction severed:
 *   J   cross-file fixed-point backfill: a boundary whose logical parent does
 *       not reach a root in-file gets its pre-compaction history
 *       prepended from the sibling that contributes the most new records (the
 *       deepest ancestor), repeated until nothing dangles or nothing new adds.
 *   K1  phantom backfill: a boundary whose logical parent is on NO record gets
 *       its pre-boundary content prepended from a sibling that DOES carry it.
 *   K2  seam ghost / K3 seamClean+bridge ghost: planted markers showing where a
 *       compaction was crossed and how.
 *
 * SSOT: every "is this a boundary / a root / a ghost / which edge" decision goes
 * through $pfg (src/pfg-core.js), never re-decided inline.
 *
 * Vendor deps are referenced through the _pv_ namespace so the apply driver can
 * DERIVE the set by grepping /_pv_\w+/ and bind each to the current minified name
 * via a discovered alias prologue. Each dep's identity (its meaning, kind, and
 * per-target locator) lives ONCE, in the anchor registry (pfg/anchors.py). This
 * file names deps abstractly and does NOT restate their meaning, so the core and
 * the registry cannot silently drift apart. Restating the semantics here would be
 * the same hand-maintained-double disease the _pv_ grep exists to kill.
 */
async function d1e(_session, _opts) {
  if (!_pv_Nk(_session)) return [];
  let _meta = await _pv_qAe(_session, _opts?.dir);
  if (!_meta) return [];
  let _buf = await _pv_r1e(_meta.filePath, _meta.fileSize);
  if (!_buf) return [];

  let _tStart = Date.now();
  let _parsed = await _pv_n1e(_buf);
  let _tParsed = Date.now();

  // Uuids present so far. _seen grows as backfill prepends sibling records;
  // _inFileUuids is frozen at the original file's contents (for provenance).
  let _seen = new Set(_parsed.map((_rec) => _rec.uuid));
  let _inFileUuids = new Set(_seen);

  let _dir = _pv_zn.dirname(_meta.filePath);
  // Determinism: the reconstruction must be a pure function of the input corpus
  // bytes. readdir order is filesystem-dependent and file mtime is not an input
  // at all, so neither may drive the ancestor pick. Sort by filename, so the same
  // set of .jsonl files yields the same sibling order, and the same pick, on any
  // machine.
  let _entries = (await _pv_MY.readdir(_dir)).sort();
  let _filesParsed = new Map(); // sibling path -> parsed records (parse once)
  let _bufCache = new Map();    // sibling path -> raw Buffer
  let _strCache = new Map();    // sibling path -> utf-8 string (cheap uuid scan)
  let _jPrepended = 0;
  // Lpus J backfilled where MORE THAN ONE sibling carried a DIVERGENT pre-compaction
  // history (one holds a record another lacks): the volume-based pick is then one of
  // several possible reconstructions, so the K3 bridge ghost must disclose it. Mirrors
  // K1's _ambiguousPhantomLpus for phantoms; without it J could splice foreign fork history into
  // a confident unmarked "reconstructed" render.
  let _ambiguousJLpus = new Set();
  let _preservedCyclesRepaired = 0;

  // ---- reconstruction pipeline. The stages below run in a required order; each
  // ---- consumes the mutated _parsed the prior stages produced, so reordering them changes
  // ---- the result or reintroduces a bug:
  // ----   F (fork backfill) + J (sibling backfill) run as a JOINT fixed point (a J-revealed
  // ----     fork origin must be F-investigated and vice versa, so they iterate until _parsed
  // ----     stops growing) BEFORE P/K, or K would fire on a phantom a sibling still holds.
  // ----   P repairs only the strict lpu-into-preserved-tail cycle after siblings have had
  // ----     their chance to supply the real ancestry, and before K classifies the boundary.
  // ----   K1 (phantom backfill) BEFORE K2 (seam) BEFORE K3 (bridge): K1 may resolve a phantom
  // ----     from a sibling (no seam); only a phantom still absent after K1 becomes a K2 seam;
  // ----     K3 only decorates a boundary whose lpu is now present.
  // ----   Divergent-edge resolution runs LAST (after K), over the fully assembled set, so it
  // ----     resolves the final edges the render walks, not an intermediate state.

  // ---- F: fork source backfill (forkedFrom, investigate-don't-guess) ------
  // A structural origin carrying forkedFrom is a COPY of a turn from a source session
  // (a /branch). If the branch was taken mid-conversation, the source holds the turns
  // BEFORE the fork point; without them this origin masquerades as the root (the
  // fork axis of the false-success). Investigate rather than guess: read the source and,
  //   - if it holds CANONICAL ancestry (a main-line turn before the copy), prepend it and
  //     re-root the copy onto its real source parent, so the walk reaches the SOURCE's own
  //     origin and the normal green/marker verdict applies;
  //   - if the copy has NO canonical turn before it in the source (empty / system-only
  //     pre-fork region), the fork is COMPLETE and this copy IS the origin: flag it green;
  //   - if the source is off-disk we cannot investigate: leave the forkedFrom in place and
  //     i1e fires the LOUD MARKER (undecidable off-disk is a hard failure, not a soft note).
  // Fixed-point: a source that is itself a fork recurses. Guards `__pfgkForkChecked`
  // so each copy is investigated once. 0-observed in the corpus (its forks are
  // compacted-boundary heads), the honest guard for the shape rather than a hedge.
  // Runs to its own fixed point. The joint loop below re-runs it after J, because a
  // J sibling-prepend can add a fork origin this then has to resolve.
  async function _backfillForkSources() {
    for (let _pass = 0; _pass < 100000; _pass++) {
      let _madeProgress = false;
      for (let _copy of _parsed) {
        if (!$pfg.isOrigin(_copy) || !_copy.forkedFrom || !_copy.forkedFrom.sessionId || _copy.__pfgkForkChecked) continue;
        let _sourcePath = _pv_zn.join(_dir, _copy.forkedFrom.sessionId + ".jsonl");
        if (_sourcePath === _meta.filePath) { _copy.__pfgkForkChecked = true; continue; }
        let _sourceMsgs = _filesParsed.get(_sourcePath);
        if (!_sourceMsgs) {
          try {
            let _sourceBuf = await _pv_MY.readFile(_sourcePath);
            _sourceMsgs = await _pv_n1e(_sourceBuf);
            _filesParsed.set(_sourcePath, _sourceMsgs);
          } catch { _copy.__pfgkForkChecked = true; continue; } // off-disk: undecidable, leave for the marker
        }
        // Key the source lookup on the ATTESTED forkedFrom.messageUuid (fall back to the
        // copy's own uuid, identical on all real data), so a branch copy that ever gets a
        // fresh uuid is still found in the source rather than silently no-op'ing to a marker.
        let _forkIdx = -1, _forkPointUuid = _copy.forkedFrom.messageUuid || _copy.uuid;
        for (let _i = 0; _i < _sourceMsgs.length; _i++)
          if (_sourceMsgs[_i].uuid === _forkPointUuid) { _forkIdx = _i; break; }
        _copy.__pfgkForkChecked = true;
        if (_forkIdx < 0) continue; // copy absent from source: leave for the marker
        // Complete fork iff the source's fork point is ITSELF a genuine origin
        // (classifyRoot reachedRoot): a real first prompt, with NO ancestry before it,
        // not a preamble, not itself a fork. The old isMain-position scan missed a
        // compact_boundary before the fork point (non-isMain, yet off-disk ancestry via
        // its lpu), wrongly flagging a boundary-headed source complete: a false success,
        // the fork analogue of the preamble trap (docs/invariant.md).
        // (Branch at the source's very start is the _forkIdx === 0 origin case.)
        let _forkPointVerdict = $pfg.classifyRoot(_sourceMsgs[_forkIdx]);
        if (_forkPointVerdict.reachedRoot) { _copy.__pfgkForkComplete = true; continue; }
        // NESTED fork AT the fork point itself (the _forkIdx === 0 sub-case the prepend
        // cannot reach: the source's fork point is its own fork origin, sharing this copy's
        // uuid). Chain: redirect this copy at the grand-source and re-investigate next pass,
        // so an N-level on-disk fork chain resolves to the real origin instead of a premature
        // marker (forbidden-middle #2). Terminates: each hop consumes a distinct on-disk
        // source. (A fork ancestor one level DOWN, _forkIdx > 0, is already handled by the
        // prepend below, which prepends the source's own fork origin for the next pass.)
        if (_forkPointVerdict.reason === "fork") {
          _copy.forkedFrom = _sourceMsgs[_forkIdx].forkedFrom;
          _copy.__pfgkForkChecked = false;
          _madeProgress = true;
          continue;
        }
        // else (none / preamble): the fork point has ancestry (a canonical parent or a
        // boundary lpu). Prepend the source's pre-fork ancestry and re-root; i1e fires the
        // marker if that ancestry is off disk.
        let _preForkAncestry = [];
        for (let _i = 0; _i < _forkIdx; _i++) {
          let _rec = _sourceMsgs[_i];
          if (_rec && _rec.uuid && !_seen.has(_rec.uuid)) { _preForkAncestry.push(_rec); _seen.add(_rec.uuid); }
        }
        _copy.parentUuid = _sourceMsgs[_forkIdx].parentUuid ?? null; // re-root onto the real source parent
        _copy.logicalParentUuid = _sourceMsgs[_forkIdx].logicalParentUuid ?? _copy.logicalParentUuid ?? null;
        if (_preForkAncestry.length > 0) _parsed = [..._preForkAncestry, ..._parsed];
        _madeProgress = true;
      }
      if (!_madeProgress) break;
    }
  }

  // ---- J: cross-file fixed-point backfill --------------------------------
  // A boundary "dangles" when its logical parent does not reach an origin
  // within the records we hold. Not merely "lpu absent": a resume imports the
  // parent's post-compaction working set INTO the child, so the lpu target can be
  // present yet its chain still truncated. $pfg.walkToRoot follows the real edge
  // (parent, else logical parent) and reads content, so a continuation preamble
  // does NOT count as a reached root and the search keeps going past it, exactly
  // as the invariant requires.
  // J iterates to a FIXED POINT: each pass prepends the deepest new ancestor and
  // may reveal a deeper dangling boundary, so the pass count is the compaction-chain
  // DEPTH, not a constant. It terminates because every continuing pass adds at least
  // one new record to _seen (the _newPrepend.length===0 break) over a finite corpus,
  // and sibling reads are cached. The high bound is a pure infinite-loop backstop: a
  // small fixed cap could cut a still-progressing deep recovery and fire the loud
  // marker on a recoverable chain (invariant forbidden-middle #2), so it must sit far
  // above any real chain depth rather than at a hand-picked number.
  async function _backfillSiblings() {
    for (let _pass = 0; _pass < 100000; _pass++) {
      let _index = $pfg.byUuid(_parsed);
      let _dangling = [];
      for (let _rec of _parsed)
        if ($pfg.isBoundary(_rec) && !_rec.parentUuid && _rec.logicalParentUuid) {
          let _boundaryLpu = _rec.logicalParentUuid;
          if (!_seen.has(_boundaryLpu) || !$pfg.walkToRoot(_boundaryLpu, _index).reachedRoot)
            _dangling.push(_boundaryLpu);
        }
      if (_dangling.length === 0) break;

      // For each dangling lpu, pick the sibling whose [0..idx(lpu)] prefix adds the
      // MOST not-yet-seen records: the deepest ancestor, not just the first match.
      let _deepestByFile = new Map();
      for (let _lpu of new Set(_dangling)) {
        let _bestPath = null, _bestIdx = -1, _mostNew = 0, _candidatePreSets = [], _bestPreSet = null;
        for (let _name of _entries) {
          if (!_name.endsWith(".jsonl")) continue;
          let _path = _pv_zn.join(_dir, _name);
          if (_path === _meta.filePath) continue;
          let _siblingMsgs = _filesParsed.get(_path);
          if (!_siblingMsgs) {
            let _siblingText = _strCache.get(_path);
            let _siblingBuf = _bufCache.get(_path);
            if (_siblingText === void 0) {
              _siblingBuf = await _pv_MY.readFile(_path);
              _bufCache.set(_path, _siblingBuf);
              _siblingText = _siblingBuf.toString("utf-8");
              _strCache.set(_path, _siblingText);
            }
            if (!_siblingText.includes(`"uuid":"${_lpu}"`)) continue;
            _siblingMsgs = await _pv_n1e(_siblingBuf);
            _filesParsed.set(_path, _siblingMsgs);
          }
          let _lpuIdx = -1;
          for (let _i = 0; _i < _siblingMsgs.length; _i++)
            if (_siblingMsgs[_i].uuid === _lpu) { _lpuIdx = _i; break; }
          if (_lpuIdx === -1) continue;
          let _newCount = 0, _preSet = new Set();
          for (let _i = 0; _i <= _lpuIdx; _i++) {
            let _rec = _siblingMsgs[_i];
            if (_rec && _rec.uuid) { _preSet.add(_rec.uuid); if (!_seen.has(_rec.uuid)) _newCount++; }
          }
          _candidatePreSets.push(_preSet);
          if (_newCount > _mostNew) { _mostNew = _newCount; _bestPath = _path; _bestIdx = _lpuIdx; _bestPreSet = _preSet; }
        }
        // Ambiguous iff >1 sibling carries this lpu AND the winner does NOT subsume the
        // others: a losing sibling holds a pre-boundary record the winner lacks, i.e. a
        // DIVERGENT fork, so the volume pick is one of several reconstructions. Nested
        // candidates (the winner IS the superset) are not ambiguous. Mirrors K1's
        // _ambiguousPhantomLpus; the K3 bridge ghost discloses it.
        if (_candidatePreSets.length > 1 && _bestPreSet) {
          let _union = new Set();
          for (let _set of _candidatePreSets) for (let _uuid of _set) _union.add(_uuid);
          if (_bestPreSet.size < _union.size) _ambiguousJLpus.add(_lpu);
        }
        if (_bestPath) {
          let _prev = _deepestByFile.get(_bestPath);
          if (_prev === void 0 || _bestIdx > _prev) _deepestByFile.set(_bestPath, _bestIdx);
        }
      }
      if (_deepestByFile.size === 0) break;

      let _newPrepend = [];
      for (let [_path, _deepestIdx] of _deepestByFile) {
        let _siblingMsgs = _filesParsed.get(_path);
        for (let _i = 0; _i <= _deepestIdx; _i++) {
          let _rec = _siblingMsgs[_i];
          if (_rec && !_seen.has(_rec.uuid)) { _newPrepend.push(_rec); _seen.add(_rec.uuid); }
        }
      }
      if (_newPrepend.length === 0) break;
      _jPrepended += _newPrepend.length;
      _parsed = [..._newPrepend, ..._parsed];
    }
  }

  // ---- F + J JOINT fixed point ----------------------------------------------
  // F is UPSTREAM of J: a fork origin that appears only via a J sibling-prepend (the
  // backfill sibling's OWN origin is a fork copy) would never be F-investigated in one
  // F-then-J sequence, firing a premature marker even with the grand-source on disk
  // (invariant forbidden-middle #2). Re-run F then J until a full round adds no record:
  // J reveals F work (a prepended fork origin) and F's prepend reveals J work (a
  // dangling boundary J then catches), so a single re-run of either is not enough. Any
  // forkedFrom still unresolved once a round is empty is genuinely off disk and
  // correctly marks. The high bound is a pure infinite-loop backstop; each real round
  // adds at least one record over a finite corpus.
  for (let _outer = 0; _outer < 100000; _outer++) {
    let _beforeLen = _parsed.length;
    await _backfillForkSources();
    await _backfillSiblings();
    if (_parsed.length === _beforeLen) break;
  }

  // ---- P: preserved-tail cycle repair ---------------------------------------
  // One compaction shape reuses the preserved-window records under the new compact
  // summary while the boundary's logicalParentUuid still points at that window's tail:
  //
  //   boundary --lpu--> tail -> ... -> head -> summary -> boundary
  //
  // The target uuid therefore EXISTS, but its walk cycles. J correctly searches
  // siblings because it does not reach an origin; K2 then ignores it because it is
  // not phantom, and K3 used to call it seamClean before i1e finally detected the
  // cycle. The compaction metadata carries enough information for a narrow repair:
  // the complete preserved order and, when the boundary is not first in the file,
  // the immediately preceding in-file record. Rebuild that window onto the predecessor
  // and put the marker on the repaired canonical path. Do not generalise this to every
  // cycle: all predicates below describe the measured lpu-into-preserved-tail shape.
  {
    let _index = $pfg.byUuid(_parsed);
    for (let _i = 0; _i < _parsed.length; _i++) {
      let _boundary = _parsed[_i];
      if (!$pfg.isBoundary(_boundary) || _boundary.parentUuid != null || !_boundary.logicalParentUuid) continue;
      let _preserved = _boundary.compactMetadata?.preservedMessages,
        _segment = _boundary.compactMetadata?.preservedSegment,
        _uuids = _preserved?.uuids;
      if (!Array.isArray(_uuids) || _uuids.length === 0 || _uuids.some((_uuid) => !_index.has(_uuid))) continue;
      let _lpu = _boundary.logicalParentUuid,
        _headUuid = _uuids[0],
        _tailUuid = _uuids.at(-1);
      if (_lpu !== _tailUuid || (_segment?.tailUuid != null && _segment.tailUuid !== _tailUuid)) continue;
      let _lpuWalk = $pfg.walkToRoot(_lpu, _index);
      if (_lpuWalk.reason !== "cycle" || !_lpuWalk.path.some((_rec) => _rec.uuid === _boundary.uuid)) continue;

      let _window = new Set(_uuids), _predUuid = null;
      for (let _j = _i - 1; _j >= 0; _j--) {
        let _uuid = _parsed[_j]?.uuid;
        if (_uuid && _inFileUuids.has(_uuid) && !_window.has(_uuid)) { _predUuid = _uuid; break; }
      }
      if (!_predUuid) continue; // boundary-at-file-start still needs J/K1/K2 evidence

      // The positional connection is trusted only when the predecessor reaches the
      // file's one fork-free origin without crossing a foreign fork or unverified seam.
      let _ownOrigins = _parsed.filter((_rec) =>
          $pfg.isOrigin(_rec) && !(_rec.forkedFrom && _rec.forkedFrom.sessionId)),
        _predWalk = $pfg.walkToRoot(_predUuid, _index),
        _proven =
          _ownOrigins.length === 1 &&
          _predWalk.reachedRoot &&
          !!_predWalk.terminus &&
          _predWalk.terminus.uuid === _ownOrigins[0].uuid &&
          !_predWalk.path.some((_rec) => _rec.forkedFrom && _rec.forkedFrom.sessionId) &&
          !$pfg.crossedUnprovenSeam(_predWalk.path),
        _kind = _proven ? "seamClean" : "seam",
        _ghostUuid = $pfg.GHOST_PREFIX + _kind + "-" + _boundary.uuid.slice(0, 8);
      if (_seen.has(_ghostUuid)) continue;

      let _payload = _proven
        ? {
            kind: "seamClean",
            badge: "◇ IN-FILE COMPACTION",
            headline: "Compaction event · preserved window repaired in-file",
            rows: [
              ["boundary", String(_boundary.uuid).slice(0, 8)],
              ["preserved messages", String(_uuids.length)],
              ["reconnected to", String(_predUuid).slice(0, 8)],
            ],
            body:
              "The compactor placed its preserved messages under the compact summary while the boundary still pointed at their tail, creating a cycle. Patch K restored the recorded preserved-message order and reconnected its head to the preceding in-file turn.",
          }
        : {
            kind: "seam",
            rows: [
              ["cyclic preserved tail", String(_lpu).slice(0, 8) + " ↻"],
              ["reattached to", String(_predUuid)],
              ["preserved messages", String(_uuids.length)],
            ],
            body:
              "The compactor placed its preserved messages under the compact summary while the boundary still pointed at their tail, creating a cycle. Patch K restored the recorded preserved-message order and reattached its head to the preceding in-file record, but that positional predecessor did not prove one unambiguous origin.",
          };
      let _ghost = {
        type: "user",
        uuid: _ghostUuid,
        parentUuid: _predUuid,
        sessionId: _boundary.sessionId,
        timestamp: _boundary.timestamp,
        message: { role: "user", content: $pfg.PFGK1_PREFIX + JSON.stringify(_payload) },
        ...(!_proven ? { __pfgkSeam: { reattachTarget: _predUuid, proven: false } } : {}),
      };

      // Write one parent choice onto every occurrence of each preserved uuid. d1e's
      // later divergent-edge pass then sees no stale alternative to select last-wins.
      let _parentCursor = _ghostUuid;
      for (let _uuid of _uuids) {
        for (let _rec of _parsed) if (_rec.uuid === _uuid) _rec.parentUuid = _parentCursor;
        _parentCursor = _uuid;
      }
      _boundary.__pfgkPreservedCycleRepair = {
        headUuid: _headUuid,
        tailUuid: _tailUuid,
        predecessorUuid: _predUuid,
        proven: _proven,
      };
      _parsed.splice(_i, 0, _ghost);
      _seen.add(_ghostUuid);
      _preservedCyclesRepaired++;
      _i++;
      _index = $pfg.byUuid(_parsed);
    }
  }

  // Provenance: which file each uuid came from (in-file first, then siblings).
  let _prov = new Map();
  for (let _uuid of _inFileUuids) _prov.set(_uuid, _meta.filePath);
  if (_filesParsed)
    for (let [_siblingPath, _siblingMsgs] of _filesParsed)
      for (let _rec of _siblingMsgs)
        if (_rec.uuid && !_prov.has(_rec.uuid)) _prov.set(_rec.uuid, _siblingPath);
  let _tAfterBackfill = Date.now();

  // ---- K1: phantom backfill ---------------------------------------------
  // A boundary whose logical parent is on no record we hold. Find a sibling that
  // carries that lpu as ITS boundary and prepend the sibling's pre-boundary
  // content, validated: the sibling must have a real turn before the boundary
  // and an origin of its own.
  let _ambiguousPhantomLpus = new Set();
  let _k1Sources = new Map();
  let _phantomTotal = 0;
  {
    let _phantomLpus = new Set();
    for (let _rec of _parsed)
      if ($pfg.isBoundary(_rec) && !_rec.parentUuid && _rec.logicalParentUuid && !_seen.has(_rec.logicalParentUuid))
        _phantomLpus.add(_rec.logicalParentUuid);
    _phantomTotal = _phantomLpus.size;
    for (let _phantomLpu of _phantomLpus) {
      let _bestPreBoundary = null, _bestSrcName = null, _qualifyingCount = 0;
      for (let _name of _entries) {
        if (!_name.endsWith(".jsonl")) continue;
        let _path = _pv_zn.join(_dir, _name);
        if (_path === _meta.filePath) continue;
        let _siblingMsgs = _filesParsed.get(_path);
        if (!_siblingMsgs) {
          try {
            let _siblingBuf = await _pv_MY.readFile(_path);
            let _siblingText = _siblingBuf.toString("utf-8");
            if (!_siblingText.includes(`"logicalParentUuid":"${_phantomLpu}"`)) continue;
            _siblingMsgs = await _pv_n1e(_siblingBuf);
            _filesParsed.set(_path, _siblingMsgs);
          } catch { continue; }
        } else {
          let _carriesLpu = false;
          for (let _sibRec of _siblingMsgs)
            if ($pfg.isBoundary(_sibRec) && _sibRec.logicalParentUuid === _phantomLpu) { _carriesLpu = true; break; }
          if (!_carriesLpu) continue;
        }
        // Locate the boundary carrying this lpu in the sibling.
        let _boundaryIdx = -1;
        for (let _j = 0; _j < _siblingMsgs.length; _j++) {
          let _sibRec = _siblingMsgs[_j];
          if ($pfg.isBoundary(_sibRec) && _sibRec.logicalParentUuid === _phantomLpu) { _boundaryIdx = _j; break; }
        }
        if (_boundaryIdx <= 0) continue;
        // The sibling must actually carry pre-boundary conversation...
        let _hasPreBoundaryTurn = false;
        for (let _j = 0; _j < _boundaryIdx; _j++)
          if ($pfg.isMain(_siblingMsgs[_j])) { _hasPreBoundaryTurn = true; break; }
        if (!_hasPreBoundaryTurn) continue;
        // ...and have an origin of its own. TIGHTENED to $pfg.isOrigin: the
        // old inline test accepted "no parent, no lpu, main turn" and so would
        // treat a compaction summary or a planted ghost as the first child.
        let _siblingOrigin = null;
        for (let _j = 0; _j < _siblingMsgs.length; _j++)
          if ($pfg.isOrigin(_siblingMsgs[_j])) { _siblingOrigin = _siblingMsgs[_j]; break; }
        if (!_siblingOrigin) continue;
        _qualifyingCount++;
        // Collect this candidate's not-yet-seen pre-boundary content WITHOUT
        // mutating the shared _seen (mirror J's non-mutating comparison at line ~103).
        // _localSeen dedups within this one sibling; _seen stays the same baseline for
        // every candidate, so a LOSING candidate leaves no record marked seen-but-
        // never-prepended, which would otherwise strand it and orphan the K2/K3 ghosts
        // and bias the length comparison toward whichever sibling was scanned first.
        let _preBoundary = [], _localSeen = new Set();
        for (let _j = 0; _j < _boundaryIdx; _j++) {
          let _sibRec = _siblingMsgs[_j];
          if (_sibRec && _sibRec.uuid && !_seen.has(_sibRec.uuid) && !_localSeen.has(_sibRec.uuid)) { _preBoundary.push(_sibRec); _localSeen.add(_sibRec.uuid); }
        }
        if (_preBoundary.length === 0) continue;
        if (!_bestPreBoundary || _preBoundary.length > _bestPreBoundary.length) { _bestPreBoundary = _preBoundary; _bestSrcName = _name; }
      }
      if (_bestPreBoundary) {
        for (let _sibRec of _bestPreBoundary) _seen.add(_sibRec.uuid); // commit ONLY the winner, now the comparison is done
        _parsed = [..._bestPreBoundary, ..._parsed];
        _k1Sources.set(_phantomLpu, { src: _bestSrcName, count: _bestPreBoundary.length, candidates: _qualifyingCount });
      }
      if (_qualifyingCount > 1) _ambiguousPhantomLpus.add(_phantomLpu);
    }
  }
  let _tAfterK1 = Date.now();

  // K1 may have parsed new sibling files; re-extend provenance so a boundary from
  // a K1-only sibling is not misread as in-file (L4).
  if (_filesParsed)
    for (let [_siblingPath, _siblingMsgs] of _filesParsed)
      for (let _rec of _siblingMsgs)
        if (_rec.uuid && !_prov.has(_rec.uuid)) _prov.set(_rec.uuid, _siblingPath);

  // ---- K2: seam ghost for a still-phantom boundary ----------------------
  // Logical parent still absent from every record: reattach the chain to the
  // in-file predecessor and plant a seam marker explaining the write-side gap.
  // Seam-trust evidence (task 28): count the forkedFrom-free own-origins ONCE. A seam
  // promotes to a trusted bridge (no marker) only when there is EXACTLY ONE, so the
  // positional reattach cannot land on the wrong own-branch; the multi-origin tail
  // (~1.7% of files) has >1 and stays a marker. Stable across the loop below: a seam
  // ghost carries a parentUuid, so it is never itself an origin.
  let _ownOrigins = _parsed.filter((_rec) => $pfg.isOrigin(_rec) && !(_rec.forkedFrom && _rec.forkedFrom.sessionId));
  let _uniqueOwnOrigin = _ownOrigins.length === 1 ? _ownOrigins[0].uuid : null;
  let _kFired = false, _kAttempted = false; // vestigial telemetry hooks; kept as-is
  for (let _i = 0; _i < _parsed.length; _i++) {
    let _rec = _parsed[_i];
    if ($pfg.isBoundary(_rec) && !_rec.parentUuid && _rec.logicalParentUuid && !_seen.has(_rec.logicalParentUuid)) {
      _kAttempted = true;
      let _predUuid = null;
      for (let _j = _i - 1; _j >= 0; _j--)
        if (_parsed[_j].uuid) { _predUuid = _parsed[_j].uuid; break; }
      if (!_predUuid) continue;
      // Does the reattach target PROVABLY rejoin THIS conversation? Only if it walks to the
      // unique own-origin crossing no forkedFrom copy (foreign lineage) and no other
      // UNVERIFIED seam (an unproven hop). $pfg.rootTrusted (in i1e) then folds `proven`
      // across every seam on the live leaf's path, so a chain of seams needs all proven.
      let _proven = false;
      if (_uniqueOwnOrigin) {
        let _targetWalk = $pfg.walkToRoot(_predUuid, $pfg.byUuid(_parsed));
        _proven =
          _targetWalk.reachedRoot &&
          !!_targetWalk.terminus &&
          _targetWalk.terminus.uuid === _uniqueOwnOrigin &&
          !_targetWalk.path.some((_pathRec) => _pathRec.forkedFrom && _pathRec.forkedFrom.sessionId) &&
          !_targetWalk.path.some((_pathRec) => $pfg.ghostKind(_pathRec) === "seam");
      }
      let _seamUuid = $pfg.GHOST_PREFIX + "seam-" + _rec.uuid.slice(0, 8);
      if (_seen.has(_seamUuid)) continue; // dedup: two boundaries sharing an 8-hex uuid prefix (mirror K3)
      let _origLpu = _rec.logicalParentUuid;
      let _ghostContent = $pfg.PFGK1_PREFIX + JSON.stringify({
        kind: "seam",
        rows: [
          ["missing phantom", String(_origLpu) + " ✗"],
          ["reattached to", String(_predUuid)],
          ["bug origin", "compact.ts:598 (write-side)"],
        ],
        body:
          (_ambiguousPhantomLpus && _ambiguousPhantomLpus.has(_origLpu)
            ? "⚠ AMBIGUOUS RECONSTRUCTION: multiple sibling files qualified for backfill at this compaction event; the prepended pre-compaction content is one of several possible reconstructions. "
            : "") +
          "Claude Code compacted the conversation here. The compactor referenced a chain predecessor never persisted to disk (write-side bug, compact.ts:598). Patch K reconnected the chain to the nearest preceding record (earlier in this file, or prepended from a sibling).",
      });
      let _ghost = {
        type: "user",
        uuid: _seamUuid,
        parentUuid: _predUuid,
        sessionId: _rec.sessionId,
        timestamp: _rec.timestamp,
        message: { role: "user", content: _ghostContent },
        // Seam-trust evidence read by $pfg.rootTrusted at render: proven=true promotes this
        // unverified positional reattach to a trusted bridge (no marker). See the guard above.
        __pfgkSeam: { reattachTarget: _predUuid, proven: _proven },
      };
      _parsed.splice(_i, 0, _ghost);
      _rec.logicalParentUuid = _seamUuid;
      _seen.add(_ghost.uuid);
      _kFired = true;
      _i++;
    }
  }

  // ---- K3: seamClean (in-file) / bridge (cross-file) ghost --------------
  // Logical parent is present, reaches an origin, and is not itself a ghost: mark
  // whether the pre-boundary lineage was recovered in-file or from a sibling. Mere
  // uuid presence is insufficient; a preserved-tail target can exist yet cycle.
  for (let _i = 0; _i < _parsed.length; _i++) {
    let _rec = _parsed[_i];
    if (
      $pfg.isBoundary(_rec) && !_rec.parentUuid && _rec.logicalParentUuid &&
      !_rec.__pfgkPreservedCycleRepair &&
      _seen.has(_rec.logicalParentUuid) && !$pfg.isGhostUuid(_rec.logicalParentUuid) &&
      $pfg.walkToRoot(_rec.logicalParentUuid, $pfg.byUuid(_parsed)).reachedRoot
    ) {
      let _origLpu = _rec.logicalParentUuid;
      let _lpuFile = _prov.get(_origLpu) || _meta.filePath;
      let _boundaryFile = _prov.get(_rec.uuid) || _meta.filePath;
      let _crossFile = _boundaryFile !== _lpuFile;
      let _crossFileSrc = _crossFile ? _pv_zn.basename(_lpuFile) : null;
      let _ghostUuid = $pfg.GHOST_PREFIX + (_crossFile ? "bridge-" : "seamClean-") + _rec.uuid.slice(0, 8);
      if (_seen.has(_ghostUuid)) continue;
      let _ghostPayload = _crossFile
        ? {
            kind: "bridge",
            badge: "↻ CROSS-FILE BRIDGE",
            headline: "Compaction origin · bridged from a sibling conversation",
            rows: [
              ["phantom (J-resolved)", String(_origLpu).slice(0, 8) + " ✗"],
              ["cross-file source", _crossFileSrc || "(sibling .jsonl)"],
              ["boundary", String(_rec.uuid).slice(0, 8)],
            ],
            body:
              (_ambiguousJLpus && _ambiguousJLpus.has(_origLpu)
                ? "⚠ AMBIGUOUS RECONSTRUCTION: more than one sibling file carries a divergent pre-compaction history for this boundary; the bridged content is one of several possible reconstructions. "
                : "") +
              "This compaction’s pre-boundary lineage lives in a sibling .jsonl. Patch J resolved it cross-file and Patch K bridges it in, so the chain renders whole across both files.",
          }
        : {
            kind: "seamClean",
            badge: "◇ IN-FILE COMPACTION",
            headline: "Compaction event · crossed in-file",
            rows: [
              ["boundary", String(_rec.uuid).slice(0, 8)],
              ["bridged via", "in-file predecessor"],
            ],
            body:
              "Claude Code compacted the conversation here; the messages above were bridged across this boundary in-file.",
          };
      let _ghost = {
        type: "user",
        uuid: _ghostUuid,
        parentUuid: _origLpu,
        sessionId: _rec.sessionId,
        timestamp: _rec.timestamp,
        message: { role: "user", content: $pfg.PFGK1_PREFIX + JSON.stringify(_ghostPayload) },
      };
      _parsed.splice(_i, 0, _ghost);
      _rec.logicalParentUuid = _ghostUuid;
      _seen.add(_ghost.uuid);
      _i++;
    }
  }

  // ---- Divergent-edge resolution -----------------------------------------
  // When the same uuid appears with DIFFERENT parentUuids across the assembled
  // records (a re-append that co-occurs with a backfilled sibling, say), $pfg.byUuid
  // is last-wins and would keep an ARBITRARY edge. Don't guess: try each candidate
  // edge and keep the one whose chain reaches an origin, the invariant's own
  // criterion applied to the choice. Rare (measured ~0.05% of uuids carry divergent
  // parents) and cheap, but strictly better than an arbitrary pick when it happens.
  {
    let _candidateParents = new Map(); // uuid -> the distinct parentUuids seen for it
    for (let _rec of _parsed)
      if (_rec && _rec.uuid) {
        if (!_candidateParents.has(_rec.uuid)) _candidateParents.set(_rec.uuid, new Set());
        _candidateParents.get(_rec.uuid).add(_rec.parentUuid ?? null);
      }
    let _index = $pfg.byUuid(_parsed);
    for (let [_uuid, _parents] of _candidateParents) {
      if (_parents.size <= 1) continue; // no divergence, nothing to resolve
      let _base = _index.get(_uuid), _pick = null;
      // deterministic candidate order; keep the FIRST that reaches an origin
      // codepoint (UTF-16 code-unit) order, NOT String.localeCompare: locale-dependent
      // sorting would make the divergent-edge candidate pick vary by host locale, breaking
      // the determinism invariant (same corpus bytes -> same reconstruction everywhere).
      for (let _parent of [..._parents].sort((_a, _b) => (String(_a) < String(_b) ? -1 : String(_a) > String(_b) ? 1 : 0))) {
        let _probe = new Map(_index);
        _probe.set(_uuid, { ..._base, parentUuid: _parent });
        if ($pfg.walkToRoot(_uuid, _probe).reachedRoot) { _pick = _parent; break; }
      }
      if (_pick !== null) for (let _rec of _parsed) if (_rec.uuid === _uuid) _rec.parentUuid = _pick;
    }
  }

  let _tDone = Date.now();

  let _telemetry = {
    timing: {
      parseMs: _tParsed - _tStart,
      crossFileMs: _tAfterBackfill - _tParsed,
      siblingBackfillMs: _tAfterK1 - _tAfterBackfill,
      bookendMs: _tDone - _tAfterK1,
    },
    siblingsScanned: _entries
      ? _entries.filter((_name) => _name.endsWith(".jsonl") && _pv_zn.join(_dir, _name) !== _meta.filePath).length
      : 0,
    phantomsBackfilled: _k1Sources.size,
    phantomsCouldNotBackfill: Math.max(0, _phantomTotal - _k1Sources.size),
    preservedCyclesRepaired: _preservedCyclesRepaired,
    prependedMessages:
      _jPrepended + (() => { let _sum = 0; for (let [, _srcInfo] of _k1Sources) _sum += _srcInfo.count; return _sum; })(),
    sources: (() => { let _srcByLpu = {}; for (let [_lpu, _srcInfo] of _k1Sources) _srcByLpu[_lpu] = _srcInfo.src; return _srcByLpu; })(),
    provBasenames: (() => {
      let _basenamesByUuid = {};
      if (_prov) for (let [_uuid, _path] of _prov) if (_path !== _meta.filePath) _basenamesByUuid[_uuid] = _pv_zn.basename(_path);
      return _basenamesByUuid;
    })(),
  };
  return _pv_GY(_parsed, _opts, _telemetry);
}
