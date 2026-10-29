/**
 * F1 25 Race Engineer Live Dashboard
 * Vanilla ES6 JavaScript - High-frequency WebSocket Client
 */

(function () {
  'use strict';

  // --- State & Watchdog Variables ---
  let socket = null;
  let reconnectAttempts = 0;
  let reconnectTimer = null;
  let lastPacketTime = 0;
  const NO_TELEMETRY_THRESHOLD_MS = 3000;

  function getToken() {
    const params = new URLSearchParams(window.location.search);
    const urlToken = params.get('token');
    if (urlToken) {
      try { localStorage.setItem('race_engineer_token', urlToken); } catch (e) {}
      return urlToken;
    }
    try {
      return localStorage.getItem('race_engineer_token') || '';
    } catch (e) {
      return '';
    }
  }

  // --- DOM Elements ---
  const el = {
    trackName: document.getElementById('track-name'),
    sessionStatus: document.getElementById('session-status'),
    safetyCarBanner: document.getElementById('safety-car-banner'),
    safetyCarText: document.getElementById('safety-car-text'),
    connectionBadge: document.getElementById('connection-badge'),
    connectionText: document.getElementById('connection-text'),
    noTelemetryBanner: document.getElementById('no-telemetry-banner'),
    themeToggle: document.getElementById('theme-toggle'),
    themeIcon: document.getElementById('theme-icon'),

    // Hero
    position: document.getElementById('val-position'),
    speed: document.getElementById('val-speed'),
    gear: document.getElementById('val-gear'),
    drs: document.getElementById('drs-indicator'),
    lap: document.getElementById('val-lap'),
    totalLaps: document.getElementById('val-total-laps'),
    lastLap: document.getElementById('val-last-lap'),
    stint: document.getElementById('val-stint'),

    // Gaps
    gapAheadName: document.getElementById('gap-ahead-name'),
    gapAheadTime: document.getElementById('gap-ahead-time'),
    gapAheadTrend: document.getElementById('gap-ahead-trend'),
    gapBehindName: document.getElementById('gap-behind-name'),
    gapBehindTime: document.getElementById('gap-behind-time'),
    gapBehindTrend: document.getElementById('gap-behind-trend'),

    // Tyres
    compoundBadge: document.getElementById('tyre-compound-badge'),
    tyreAge: document.getElementById('tyre-age-badge'),
    tyreWearRate: document.getElementById('tyre-wear-rate'),
    wearFL: document.getElementById('wear-fl'),
    wearFR: document.getElementById('wear-fr'),
    wearRL: document.getElementById('wear-rl'),
    wearRR: document.getElementById('wear-rr'),
    tempFL: document.getElementById('temp-fl'),
    tempFR: document.getElementById('temp-fr'),
    tempRL: document.getElementById('temp-rl'),
    tempRR: document.getElementById('temp-rr'),
    barFL: document.getElementById('bar-fl'),
    barFR: document.getElementById('bar-fr'),
    barRL: document.getElementById('bar-rl'),
    barRR: document.getElementById('bar-rr'),

    // Fuel & ERS
    fuelKg: document.getElementById('val-fuel-kg'),
    fuelDelta: document.getElementById('val-fuel-delta'),
    fuelBurn: document.getElementById('val-fuel-burn'),
    ersPct: document.getElementById('val-ers-pct'),
    ersBar: document.getElementById('ers-bar-fill'),

    // Strategy & Weather
    weatherNow: document.getElementById('val-weather-now'),
    ambientTemps: document.getElementById('val-ambient-temps'),
    nextRain: document.getElementById('val-next-rain'),
    pitWindow: document.getElementById('val-pit-window'),

    // Damage
    damageBadge: document.getElementById('damage-status-badge'),
    dmgFlWing: document.getElementById('dmg-fl-wing'),
    dmgFrWing: document.getElementById('dmg-fr-wing'),
    dmgRearWing: document.getElementById('dmg-rear-wing'),
    dmgFloor: document.getElementById('dmg-floor'),
    dmgDiffuser: document.getElementById('dmg-diffuser'),
    dmgSidepod: document.getElementById('dmg-sidepod'),
    dmgGearbox: document.getElementById('dmg-gearbox'),
    dmgEngine: document.getElementById('dmg-engine'),

    // Tower & Radio
    towerTbody: document.getElementById('timing-tower-tbody'),
    towerCount: document.getElementById('tower-count'),
    radioLog: document.getElementById('radio-log'),

    // Track Map & G-Force
    trackMapCanvas: document.getElementById('track-map-canvas'),
    mapStatusBadge: document.getElementById('map-status-badge'),
    btnRebuildMap: document.getElementById('btn-rebuild-map'),
    mapPlaceholderHint: document.getElementById('map-placeholder-hint'),
    gForceCanvas: document.getElementById('g-force-canvas'),
    valGLat: document.getElementById('val-g-lat'),
    valGLon: document.getElementById('val-g-lon'),

    // Tabs
    tabBtnLive: document.getElementById('tab-btn-live'),
    tabBtnLaps: document.getElementById('tab-btn-laps'),
    viewLive: document.getElementById('view-live'),
    viewLaps: document.getElementById('view-laps'),

    // Laps Filters & Summary
    filterTrack: document.getElementById('laps-filter-track'),
    filterSession: document.getElementById('laps-filter-session'),
    selectLapA: document.getElementById('laps-select-a'),
    selectLapB: document.getElementById('laps-select-b'),
    filterValid: document.getElementById('laps-filter-valid'),
    btnRunCompare: document.getElementById('btn-run-compare'),
    btnRefreshSessions: document.getElementById('btn-refresh-sessions'),

    summaryLapATime: document.getElementById('summary-lap-a-time'),
    summaryLapAMeta: document.getElementById('summary-lap-a-meta'),
    summaryLapBTime: document.getElementById('summary-lap-b-time'),
    summaryLapBMeta: document.getElementById('summary-lap-b-meta'),
    summaryDeltaBadge: document.getElementById('summary-delta-badge'),

    // Laps Charts & Map
    chartSpeed: document.getElementById('chart-speed'),
    chartInputs: document.getElementById('chart-inputs'),
    chartDelta: document.getElementById('chart-delta'),
    lapsMapCanvas: document.getElementById('laps-map-canvas'),
    cornerBreakdownTbody: document.getElementById('corner-breakdown-tbody'),
    cornerCountBadge: document.getElementById('corner-count-badge'),

    // Stints
    stintSelect: document.getElementById('stint-select'),
    stintTimeSlope: document.getElementById('stint-time-slope'),
    stintWearSlope: document.getElementById('stint-wear-slope'),
    stintLapCount: document.getElementById('stint-lap-count'),
    stintCompoundName: document.getElementById('stint-compound-name'),
    stintLapsTbody: document.getElementById('stint-laps-tbody'),

    // Setups
    tabBtnSetups: document.getElementById('tab-btn-setups'),
    viewSetups: document.getElementById('view-setups'),
    tabBtnTracks: document.getElementById('tab-btn-tracks'),
    viewTracks: document.getElementById('view-tracks'),
    setupsFilterTrack: document.getElementById('setups-filter-track'),
    setupsFilterClass: document.getElementById('setups-filter-class'),
    setupsFilterConditions: document.getElementById('setups-filter-conditions'),
    setupsFilterSession: document.getElementById('setups-filter-session'),
    btnSetupsLiveSync: document.getElementById('btn-setups-live-sync'),
    btnOpenNewSetup: document.getElementById('btn-open-new-setup'),
    btnOpenImport: document.getElementById('btn-open-import'),
    btnDownloadCsvTmpl: document.getElementById('btn-download-csv-tmpl'),

    mountedSetupBanner: document.getElementById('mounted-setup-banner'),
    mountedSetupName: document.getElementById('mounted-setup-name'),
    mountedSetupFp: document.getElementById('mounted-setup-fp'),
    btnEditMountedSetup: document.getElementById('btn-edit-mounted-setup'),
    btnExportMountedSheet: document.getElementById('btn-export-mounted-sheet'),
    btnExportMountedJson: document.getElementById('btn-export-mounted-json'),
    statMountedWings: document.getElementById('stat-mounted-wings'),
    statMountedDiff: document.getElementById('stat-mounted-diff'),
    statMountedBias: document.getElementById('stat-mounted-bias'),
    statMountedTyresF: document.getElementById('stat-mounted-tyres-f'),
    statMountedTyresR: document.getElementById('stat-mounted-tyres-r'),

    // Setup Form Modal
    setupFormModal: document.getElementById('setup-form-modal'),
    setupFormModalTitle: document.getElementById('setup-form-modal-title'),
    setupFormModeBadge: document.getElementById('setup-form-mode-badge'),
    btnCloseSetupFormModal: document.getElementById('btn-close-setup-form-modal'),
    btnCancelSetupForm: document.getElementById('btn-cancel-setup-form'),
    btnClearSetupForm: document.getElementById('btn-clear-setup-form'),
    btnSaveSetupForm: document.getElementById('btn-save-setup-form'),
    btnStartBlank: document.getElementById('btn-start-blank'),
    btnStartCopyCurrent: document.getElementById('btn-start-copy-current'),
    selectDuplicateSetup: document.getElementById('select-duplicate-setup'),
    btnSyncLiveMeta: document.getElementById('btn-sync-live-meta'),
    setupInputName: document.getElementById('setup-input-name'),
    setupInputClass: document.getElementById('setup-input-class'),
    setupInputTrack: document.getElementById('setup-input-track'),
    setupInputConditions: document.getElementById('setup-input-conditions'),
    setupInputSession: document.getElementById('setup-input-session'),
    setupInputTeam: document.getElementById('setup-input-team'),
    setupInputSourceNote: document.getElementById('setup-input-source-note'),
    setupInputSourceLink: document.getElementById('setup-input-source-link'),
    setupInputNotes: document.getElementById('setup-input-notes'),
    setupFormGroupsWrap: document.getElementById('setup-form-groups-wrap'),
    setupDraftStatus: document.getElementById('setup-draft-status'),
    setupReadOnlyNotice: document.getElementById('setup-read-only-notice'),

    // Duplicate Modal
    setupDuplicateModal: document.getElementById('setup-duplicate-modal'),
    dupModalFp: document.getElementById('dup-modal-fp'),
    dupExistingName: document.getElementById('dup-existing-name'),
    dupExistingMeta: document.getElementById('dup-existing-meta'),
    btnDupOpenExisting: document.getElementById('btn-dup-open-existing'),
    btnDupSaveAnyway: document.getElementById('btn-dup-save-anyway'),
    btnDupCancel: document.getElementById('btn-dup-cancel'),
    btnCloseDupModal: document.getElementById('btn-close-dup-modal'),

    // Track Notes Editor
    tracksSearchInput: document.getElementById('tracks-search-input'),
    tracksListContainer: document.getElementById('tracks-list-container'),
    tracksCountBadge: document.getElementById('tracks-count-badge'),
    trackEditorHeading: document.getElementById('track-editor-heading'),
    trackEditorFileBadge: document.getElementById('track-editor-file-badge'),
    btnTrackModeForm: document.getElementById('btn-track-mode-form'),
    btnTrackModeRaw: document.getElementById('btn-track-mode-raw'),
    trackEditorFormView: document.getElementById('track-editor-form-view'),
    trackEditorRawView: document.getElementById('track-editor-raw-view'),
    trackCornersTbody: document.getElementById('track-corners-tbody'),
    btnAddCornerRow: document.getElementById('btn-add-corner-row'),
    trackSectionOvertaking: document.getElementById('track-section-overtaking'),
    trackSectionTyres: document.getElementById('track-section-tyres'),
    trackSectionSetupNotes: document.getElementById('track-section-setup-notes'),
    trackRawContent: document.getElementById('track-raw-content'),
    trackSaveStatus: document.getElementById('track-save-status'),
    btnSaveTrackNotes: document.getElementById('btn-save-track-notes'),

    setupsRankTbody: document.getElementById('setups-rank-tbody'),
    diffSelectA: document.getElementById('diff-select-a'),
    diffSelectB: document.getElementById('diff-select-b'),
    btnRunSetupDiff: document.getElementById('btn-run-setup-diff'),
    diffResultsWrap: document.getElementById('diff-results-wrap'),
    diffChangesCount: document.getElementById('diff-changes-count'),
    diffNotes: document.getElementById('diff-notes'),
    thDiffA: document.getElementById('th-diff-a'),
    thDiffB: document.getElementById('th-diff-b'),
    setupsDiffTbody: document.getElementById('setups-diff-tbody'),

    setupDetailsModal: document.getElementById('setup-details-modal'),
    modalDetailsTitle: document.getElementById('modal-details-title'),
    modalDetailsBody: document.getElementById('modal-details-body'),
    btnCloseDetailsModal: document.getElementById('btn-close-details-modal'),
    btnCloseDetailsBtn: document.getElementById('btn-close-details-btn'),
    btnModalExportSheet: document.getElementById('btn-modal-export-sheet'),
    btnModalExportJson: document.getElementById('btn-modal-export-json'),

    setupImportModal: document.getElementById('setup-import-modal'),
    tabImportFreetext: document.getElementById('tab-import-freetext'),
    tabImportJson: document.getElementById('tab-import-json'),
    tabImportCsv: document.getElementById('tab-import-csv'),
    importName: document.getElementById('import-name'),
    importTrack: document.getElementById('import-track'),
    importClass: document.getElementById('import-class'),
    importConditions: document.getElementById('import-conditions'),
    importSession: document.getElementById('import-session'),
    importNote: document.getElementById('import-note'),
    importContent: document.getElementById('import-content'),
    importContentLabel: document.getElementById('import-content-label'),
    btnParseValidate: document.getElementById('btn-parse-validate'),
    importStatusSpinner: document.getElementById('import-status-spinner'),
    importPreviewBox: document.getElementById('import-preview-box'),
    previewValidBadge: document.getElementById('preview-valid-badge'),
    previewAlerts: document.getElementById('preview-alerts'),
    previewParamsGrid: document.getElementById('preview-params-grid'),
    btnConfirmSaveImport: document.getElementById('btn-confirm-save-import'),
    btnCloseImportModal: document.getElementById('btn-close-import-modal'),

    // Settings
    tabBtnSettings: document.getElementById('tab-btn-settings'),
    viewSettings: document.getElementById('view-settings'),
    settingPersonaName: document.getElementById('setting-persona-name') || document.getElementById('st-persona'),
    settingProfanity: document.getElementById('setting-profanity') || document.getElementById('st-prof'),
    settingReactionFreq: document.getElementById('setting-reaction-frequency') || document.getElementById('st-freq'),
    settingMinGapS: document.getElementById('setting-min-gap-s') || document.getElementById('st-gap'),
    btnSaveSettings: document.getElementById('btn-save-settings') || document.getElementById('btn-save-engineer'),
    settingsFeedbackMsg: document.getElementById('settings-feedback-msg'),

    // App Sound Test & Audio Hardware
    soundTestStatusBadge: document.getElementById('sound-test-status-badge'),
    soundTestSessionBanner: document.getElementById('sound-test-session-banner'),
    soundTestControlsContainer: document.getElementById('sound-test-controls-container'),
    selectAudioInput: document.getElementById('select-audio-input') || document.getElementById('st-in'),
    selectAudioOutput: document.getElementById('select-audio-output') || document.getElementById('st-out'),
    btnRefreshAudioDevices: document.getElementById('btn-refresh-audio-devices') || document.getElementById('btn-refresh-audio'),
    soundTestDeviceWarning: document.getElementById('sound-test-device-warning'),
    sliderVolEngineer: document.getElementById('slider-vol-engineer') || document.getElementById('v-eng'),
    labelVolEngineer: document.getElementById('label-vol-engineer') || document.getElementById('v-eng-out'),
    sliderVolSpotter: document.getElementById('slider-vol-spotter') || document.getElementById('v-spot'),
    labelVolSpotter: document.getElementById('label-vol-spotter') || document.getElementById('v-spot-out'),
    sliderVolBeeps: document.getElementById('slider-vol-beeps') || document.getElementById('v-beep'),
    labelVolBeeps: document.getElementById('label-vol-beeps') || document.getElementById('v-beep-out'),
    toggleSpeakerFx: document.getElementById('toggle-speaker-fx') || document.getElementById('chk-radio-fx'),
    btnSpeakerTest: document.getElementById('btn-speaker-test'),
    speakerTestStatus: document.getElementById('speaker-test-status'),
    btnMicTest: document.getElementById('btn-mic-test'),
    micClippingBadge: document.getElementById('mic-clipping-badge'),
    micMeterFill: document.getElementById('mic-meter-fill'),
    micMeterDb: document.getElementById('mic-meter-db'),
    micMetricsLabel: document.getElementById('mic-metrics-label'),
    micTranscriptResult: document.getElementById('mic-transcript-result'),
    btnTriggerRadioCheck: document.getElementById('btn-trigger-radio-check') || document.getElementById('btn-radio-check'),

    // Network & Ports
    settingTelemetryPort: document.getElementById('setting-telemetry-port') || document.getElementById('st-udp'),
    settingDashboardPort: document.getElementById('setting-dashboard-port') || document.getElementById('st-web'),
    settingAskPort: document.getElementById('setting-ask-port') || document.getElementById('st-ask-ports'),
    btnSavePorts: document.getElementById('btn-save-ports'),
    portsFeedbackMsg: document.getElementById('ports-feedback-msg'),

    // Usage Guard
    tabBtnUsage: document.getElementById('tab-btn-usage'),
    viewUsage: document.getElementById('view-usage'),
    btnRefreshUsage: document.getElementById('btn-refresh-usage'),
    usageLastProviderBadge: document.getElementById('usage-last-provider-badge'),
    usageCardsContainer: document.getElementById('usage-cards-container'),
    cloudQuotaPill: document.getElementById('cloud-quota-pill') || document.getElementById('ai-chip'),
    cloudQuotaText: document.getElementById('cloud-quota-text') || document.getElementById('ai-chip-text'),
    usageResetCountdown: document.getElementById('usage-reset-countdown'),
    usagePreflightBanner: document.getElementById('usage-preflight-banner'),
    usagePreflightBadge: document.getElementById('usage-preflight-badge'),
    usagePreflightMsg: document.getElementById('usage-preflight-msg'),
    usageHistoryRange: document.getElementById('usage-history-range') || document.getElementById('usage-timeframe'),
    usageHistoryTbody: document.getElementById('usage-history-tbody') || document.getElementById('history'),
  };

  // --- Track Map & G-Force State ---
  let cachedOutline = null;
  let cachedTrackId = null;
  let isFetchingOutline = false;
  let gHistory = []; // array of [lat, lon]
  let runningBounds = { minX: Infinity, maxX: -Infinity, minZ: Infinity, maxZ: -Infinity };

  // --- Helper: Lap Time Formatter ---
  function formatLapTime(seconds) {
    if (seconds === null || seconds === undefined || isNaN(seconds) || seconds <= 0) {
      return '--:--.---';
    }
    const mins = Math.floor(seconds / 60);
    const secs = (seconds % 60).toFixed(3);
    const padSecs = secs < 10 ? '0' + secs : secs;
    return mins > 0 ? `${mins}:${padSecs}` : `0:${padSecs}`;
  }

  // --- Helper: Format Gaps ---
  function formatGap(seconds) {
    if (seconds === null || seconds === undefined) return '--';
    if (seconds === 0) return 'LEADER';
    const s = Math.abs(seconds).toFixed(2);
    return `+${s}s`;
  }

  // --- Helper: Temperature Color Class ---
  function getTempClass(celsius) {
    if (!celsius || isNaN(celsius)) return 'temp-optimum';
    if (celsius < 85) return 'temp-cold';
    if (celsius <= 105) return 'temp-optimum';
    if (celsius <= 115) return 'temp-warm';
    return 'temp-hot';
  }

  // --- Helper: Wear Bar Color ---
  function getWearColor(pct) {
    if (pct < 25) return '#00e676'; // Green
    if (pct < 50) return '#ffd600'; // Yellow
    if (pct < 75) return '#ffab00'; // Orange
    return '#ff3d00'; // Red
  }

  // --- Theme Toggle Logic ---
  function initTheme() {
    const root = document.documentElement;
    const saved = localStorage.getItem('race_engineer_theme') || localStorage.getItem('re-theme');
    if (saved === 'light') {
      document.body.classList.replace('dark-theme', 'light-theme');
      root.dataset.theme = 'light';
      if (el.themeIcon) el.themeIcon.textContent = '🌙';
    } else {
      document.body.classList.replace('light-theme', 'dark-theme');
      root.dataset.theme = 'dark';
      if (el.themeIcon) el.themeIcon.textContent = '☀️';
    }

    function toggleTheme() {
      const isLight = document.body.classList.contains('light-theme') || root.dataset.theme === 'light';
      if (isLight) {
        document.body.classList.replace('light-theme', 'dark-theme');
        root.dataset.theme = 'dark';
        if (el.themeIcon) el.themeIcon.textContent = '☀️';
        try { localStorage.setItem('race_engineer_theme', 'dark'); localStorage.setItem('re-theme', 'dark'); } catch (e) {}
      } else {
        document.body.classList.replace('dark-theme', 'light-theme');
        root.dataset.theme = 'light';
        if (el.themeIcon) el.themeIcon.textContent = '🌙';
        try { localStorage.setItem('race_engineer_theme', 'light'); localStorage.setItem('re-theme', 'light'); } catch (e) {}
      }
    }

    if (el.themeToggle) el.themeToggle.addEventListener('click', toggleTheme);
    const themeBtn = document.getElementById('theme');
    if (themeBtn) themeBtn.addEventListener('click', toggleTheme);
  }

  // --- Telemetry Watchdog Timer ---
  function startWatchdog() {
    setInterval(() => {
      if (lastPacketTime === 0) return;
      const elapsed = Date.now() - lastPacketTime;
      if (elapsed > NO_TELEMETRY_THRESHOLD_MS) {
        el.noTelemetryBanner.classList.remove('hidden');
        el.connectionBadge.className = 'status-pill offline';
        el.connectionText.textContent = 'NO TELEMETRY';
      }
    }, 500);
  }

  // --- Helper: Update Top-Bar Cloud Quota Pill ---
  function updateCloudQuotaPill(quota) {
    if (!el.cloudQuotaPill || !el.cloudQuotaText) return;
    const pct = quota.pct !== undefined ? quota.pct : (quota.overall_pct || 0);
    const cutoff = quota.cutoff_pct || 80;
    el.cloudQuotaText.textContent = `CLOUD: ${pct.toFixed(0)}%`;
    el.cloudQuotaPill.className = 'status-pill cursor-pointer';
    if (pct >= cutoff) {
      el.cloudQuotaPill.classList.add('danger');
      el.cloudQuotaPill.title = `Cloud AI at safety cutoff (${pct.toFixed(1)}% >= ${cutoff}%). Using local fallback. Click to view Usage tab.`;
    } else if (pct >= 50) {
      el.cloudQuotaPill.classList.add('warning');
      el.cloudQuotaPill.title = `Cloud AI approaching cutoff (${pct.toFixed(1)}%). Safety margin at ${cutoff}%. Click to view Usage tab.`;
    } else {
      el.cloudQuotaPill.classList.add('ok');
      el.cloudQuotaPill.title = `Cloud AI Quota: ${pct.toFixed(1)}% used. Click to view Usage tab.`;
    }
  }

  // --- UI Update: Full Telemetry State ---
  function updateTelemetryUI(data) {
    const isLive = !!data.session_live;
    if (data.last_packet_time && data.last_packet_time > 0) {
      lastPacketTime = Date.now();
    }

    if (isLive || (lastPacketTime > 0 && (Date.now() - lastPacketTime) < 3500)) {
      if (el.noTelemetryBanner) el.noTelemetryBanner.classList.add('hidden');
      el.connectionBadge.className = 'status-pill connected';
      el.connectionText.textContent = 'LIVE';
      if (el.sessionStatus) el.sessionStatus.textContent = 'ACTIVE';
    } else {
      // Connected to dashboard server, waiting for game telemetry packets
      if (el.noTelemetryBanner) el.noTelemetryBanner.classList.remove('hidden');
      el.connectionBadge.className = 'status-pill standby';
      el.connectionText.textContent = 'STANDBY (WAITING FOR F1)';
      if (el.sessionStatus && !data.track) el.sessionStatus.textContent = 'STANDBY';
    }

    // Cloud AI Quota Top-Bar Pill
    if (data.cloud_quota) {
      updateCloudQuotaPill(data.cloud_quota);
    }

    // App Sound Test Session State (blocked during live session)
    if (data.session_live !== undefined) {
      updateSoundTestSessionState(data.session_live);
    }

    // 1. Session & Track
    if (data.track) {
      el.trackName.textContent = data.track;
      el.sessionStatus.textContent = 'ACTIVE';
    }

    // 2. Safety Car
    if (data.safety_car) {
      el.safetyCarBanner.classList.remove('hidden');
      el.safetyCarText.textContent = data.safety_car.toUpperCase();
      el.safetyCarBanner.className = 'sc-banner';
      if (data.safety_car.includes('virtual')) {
        el.safetyCarBanner.classList.add('sc-vsc');
      } else if (data.safety_car.includes('formation')) {
        el.safetyCarBanner.classList.add('sc-formation');
      } else {
        el.safetyCarBanner.classList.add('sc-full');
      }
    } else {
      el.safetyCarBanner.classList.add('hidden');
    }

    // 3. Hero Strip
    el.position.textContent = data.position !== null ? data.position : '--';
    el.lap.textContent = data.lap !== null ? data.lap : '--';
    el.totalLaps.textContent = data.total_laps !== null ? data.total_laps : '--';
    el.lastLap.textContent = formatLapTime(data.last_lap_s);
    if (data.pit_stops !== null) {
      el.stint.textContent = `STINT ${data.pit_stops + 1}`;
    }

    // Speed & Gear
    if (data.speed_kph !== undefined && data.speed_kph !== null) {
      el.speed.textContent = data.speed_kph;
    }
    if (data.gear !== undefined && data.gear !== null) {
      el.gear.textContent = data.gear === 0 ? 'N' : data.gear === -1 ? 'R' : data.gear;
    }
    if (data.drs) {
      el.drs.classList.add('active');
    } else {
      el.drs.classList.remove('active');
    }

    // 4. Gaps
    if (data.car_ahead) {
      el.gapAheadName.textContent = data.car_ahead.name || 'CAR AHEAD';
      el.gapAheadTime.textContent = data.car_ahead.gap_s !== null ? `+${data.car_ahead.gap_s.toFixed(2)}s` : '--';
      const trend = data.car_ahead.trend_s_per_lap;
      if (trend !== null) {
        if (trend < 0) {
          el.gapAheadTrend.className = 'trend-arrow gaining';
          el.gapAheadTrend.textContent = `▲ ${trend.toFixed(2)}s/l (gaining)`;
        } else if (trend > 0) {
          el.gapAheadTrend.className = 'trend-arrow losing';
          el.gapAheadTrend.textContent = `▼ +${trend.toFixed(2)}s/l (losing)`;
        } else {
          el.gapAheadTrend.className = 'trend-arrow neutral';
          el.gapAheadTrend.textContent = `▬ steady`;
        }
      } else {
        el.gapAheadTrend.textContent = '--';
      }
    } else {
      el.gapAheadName.textContent = 'LEADER';
      el.gapAheadTime.textContent = 'P1';
      el.gapAheadTrend.textContent = '--';
    }

    if (data.car_behind) {
      el.gapBehindName.textContent = data.car_behind.name || 'CAR BEHIND';
      el.gapBehindTime.textContent = data.car_behind.gap_s !== null ? `+${data.car_behind.gap_s.toFixed(2)}s` : '--';
      const trend = data.car_behind.trend_s_per_lap;
      if (trend !== null) {
        if (trend > 0) {
          el.gapBehindTrend.className = 'trend-arrow gaining';
          el.gapBehindTrend.textContent = `▲ +${trend.toFixed(2)}s/l (pulling away)`;
        } else if (trend < 0) {
          el.gapBehindTrend.className = 'trend-arrow losing';
          el.gapBehindTrend.textContent = `▼ ${trend.toFixed(2)}s/l (closing)`;
        } else {
          el.gapBehindTrend.className = 'trend-arrow neutral';
          el.gapBehindTrend.textContent = `▬ steady`;
        }
      } else {
        el.gapBehindTrend.textContent = '--';
      }
    } else {
      el.gapBehindName.textContent = 'NONE';
      el.gapBehindTime.textContent = '--';
      el.gapBehindTrend.textContent = '--';
    }

    // 5. Tyres
    if (data.tyres) {
      const t = data.tyres;
      if (t.compound) {
        el.compoundBadge.textContent = t.compound.toUpperCase();
        el.compoundBadge.className = `compound-badge compound-${t.compound.toLowerCase()}`;
      }
      if (t.age_laps !== null) {
        el.tyreAge.textContent = `${t.age_laps} LAPS`;
      }
      if (t.wear_pct_per_lap !== null) {
        el.tyreWearRate.textContent = `${t.wear_pct_per_lap}% / lap`;
      }

      // Corner Wear & Surface Temps
      const wear = t.wear_pct || {};
      const temps = t.surface_temp_c || {};

      const corners = [
        { id: 'fl', key: 'front left', wearEl: el.wearFL, tempEl: el.tempFL, barEl: el.barFL },
        { id: 'fr', key: 'front right', wearEl: el.wearFR, tempEl: el.tempFR, barEl: el.barFR },
        { id: 'rl', key: 'rear left', wearEl: el.wearRL, tempEl: el.tempRL, barEl: el.barRL },
        { id: 'rr', key: 'rear right', wearEl: el.wearRR, tempEl: el.tempRR, barEl: el.barRR },
      ];

      corners.forEach(c => {
        const w = wear[c.key];
        const temp = temps[c.key];

        if (w !== undefined && w !== null) {
          c.wearEl.textContent = `${Math.round(w)}%`;
          c.barEl.style.width = `${Math.min(w, 100)}%`;
          c.barEl.style.backgroundColor = getWearColor(w);
        }
        if (temp !== undefined && temp !== null) {
          c.tempEl.textContent = `${temp}°C`;
          c.tempEl.className = `temp-val mono ${getTempClass(temp)}`;
        }
      });
    }

    // 6. Fuel & ERS
    if (data.fuel) {
      el.fuelKg.textContent = `${data.fuel.kg} kg`;
      const delta = data.fuel.laps_delta;
      if (delta !== null) {
        el.fuelDelta.textContent = `${delta >= 0 ? '+' : ''}${delta.toFixed(2)} laps`;
        el.fuelDelta.style.color = delta >= 0 ? 'var(--color-green)' : 'var(--color-red)';
      }
      el.fuelBurn.textContent = data.fuel.kg_per_lap ? `${data.fuel.kg_per_lap} kg/l` : '--';
    }

    if (data.ers_pct !== null && data.ers_pct !== undefined) {
      el.ersPct.textContent = `${data.ers_pct}%`;
      el.ersBar.style.width = `${Math.min(Math.max(data.ers_pct, 0), 100)}%`;
    }

    // 7. Strategy & Weather
    if (data.weather) {
      el.weatherNow.textContent = (data.weather.now || '--').toUpperCase();
      el.ambientTemps.textContent = `Track: ${data.weather.track_c || '--'}°C | Air: ${data.weather.air_c || '--'}°C`;
      if (data.weather.next_rain) {
        el.nextRain.textContent = `RAIN in ${data.weather.next_rain.in_min}m (${data.weather.next_rain.pct}%)`;
        el.nextRain.className = 'stat-medium highlight-amber';
      } else {
        el.nextRain.textContent = 'NO RAIN FORECAST';
        el.nextRain.className = 'stat-medium highlight-cyan';
      }
    }

    if (data.pit_window) {
      el.pitWindow.textContent = `Laps ${data.pit_window.ideal_lap} - ${data.pit_window.latest_lap}`;
    } else {
      el.pitWindow.textContent = 'NOT SCHEDULED';
    }

    // 8. Damage
    const dmg = data.damage_pct || {};
    const partsMap = [
      { key: 'front left wing', el: el.dmgFlWing },
      { key: 'front right wing', el: el.dmgFrWing },
      { key: 'rear wing', el: el.dmgRearWing },
      { key: 'floor', el: el.dmgFloor },
      { key: 'diffuser', el: el.dmgDiffuser },
      { key: 'sidepod', el: el.dmgSidepod },
      { key: 'gearbox', el: el.dmgGearbox },
      { key: 'engine', el: el.dmgEngine },
    ];

    let hasDamage = false;
    partsMap.forEach(p => {
      const val = dmg[p.key] || 0;
      p.el.textContent = `${val}%`;
      const parent = p.el.closest('.dmg-item');
      if (val > 0) {
        hasDamage = true;
        parent.classList.add('damaged');
      } else {
        parent.classList.remove('damaged');
      }
    });

    if (hasDamage) {
      el.damageBadge.className = 'meta-pill pill-warn';
      el.damageBadge.textContent = 'AERO/MECH DAMAGE DETECTED';
    } else {
      el.damageBadge.className = 'meta-pill pill-ok';
      el.damageBadge.textContent = 'ALL SYSTEMS NOMINAL';
    }

    // 9. Timing Tower
    if (data.timing_tower && Array.isArray(data.timing_tower)) {
      renderTimingTower(data.timing_tower);
    }

    // 10. Track Outline & Map
    if (data.track_id !== undefined && data.track_id !== null) {
      if (data.track_id !== cachedTrackId || (data.has_track_outline && !cachedOutline)) {
        cachedTrackId = data.track_id;
        fetchTrackMap();
      }
    }

    if (data.car_positions && Array.isArray(data.car_positions)) {
      renderTrackMap(data.car_positions);
    }

    // 11. G-Force Friction Circle
    if (data.g_force) {
      renderGForce(data.g_force);
    }

    // --- Modern Pit Wall Dashboard Sync ---
    const linkText = document.getElementById('link-text');
    const linkChip = document.getElementById('link-chip');
    if (linkText && linkChip) {
      if (isLive || (lastPacketTime > 0 && (Date.now() - lastPacketTime) < 3500)) {
        linkText.textContent = 'Live · 20 Hz';
        linkChip.className = 'chip ok';
      } else {
        linkText.textContent = 'Waiting for game';
        linkChip.className = 'chip wait';
      }
    }
    const ppsEl = document.getElementById('pps');
    if (ppsEl) ppsEl.textContent = isLive ? '20' : '0';
    const lastPktEl = document.getElementById('last-packet');
    if (lastPktEl) lastPktEl.textContent = lastPacketTime > 0 ? 'just now' : 'never';

    // Hero Strip
    const hPos = document.getElementById('h-pos');
    if (hPos) hPos.textContent = data.position !== null ? `P${data.position}` : 'P--';
    const hPosS = document.getElementById('h-pos-s');
    if (hPosS && data.grid_position !== undefined && data.position !== null) {
      const diff = data.grid_position - data.position;
      hPosS.innerHTML = diff > 0 ? `<span class="up">▲ ${diff}</span> from grid` : diff < 0 ? `<span class="warn">▼ ${Math.abs(diff)}</span> from grid` : `<span>▬ 0</span> from grid`;
    }
    const hLap = document.getElementById('h-lap');
    if (hLap) {
      hLap.innerHTML = `${data.lap !== null ? data.lap : '--'}<small> / ${data.total_laps !== null ? data.total_laps : '--'}</small>`;
    }
    const hLapBar = document.getElementById('h-lap-bar');
    if (hLapBar && data.lap && data.total_laps) {
      hLapBar.style.width = `${Math.min(100, Math.round((data.lap / data.total_laps) * 100))}%`;
    }
    const hLast = document.getElementById('h-last');
    if (hLast) hLast.textContent = formatLapTime(data.last_lap_s);
    const hAhead = document.getElementById('h-ahead');
    if (hAhead) {
      hAhead.textContent = data.car_ahead && data.car_ahead.gap_s !== null ? `+${data.car_ahead.gap_s.toFixed(2)}s` : (data.position === 1 ? 'LEADER' : '--');
    }
    const hAheadS = document.getElementById('h-ahead-s');
    if (hAheadS) {
      hAheadS.textContent = data.car_ahead ? (data.car_ahead.name || 'Ahead') : '--';
    }
    const hBehind = document.getElementById('h-behind');
    if (hBehind) {
      hBehind.textContent = data.car_behind && data.car_behind.gap_s !== null ? `+${data.car_behind.gap_s.toFixed(2)}s` : '--';
    }
    const hBehindS = document.getElementById('h-behind-s');
    if (hBehindS) {
      hBehindS.textContent = data.car_behind ? (data.car_behind.name || 'Behind') : '--';
    }

    // Glance HUD
    const gPos = document.getElementById('g-pos');
    if (gPos) gPos.textContent = data.position !== null ? `P${data.position}` : 'P--';
    const gAhead = document.getElementById('g-ahead');
    if (gAhead) gAhead.textContent = data.car_ahead && data.car_ahead.gap_s !== null ? `+${data.car_ahead.gap_s.toFixed(2)}s` : (data.position === 1 ? 'P1' : '--');
    const gBehind = document.getElementById('g-behind');
    if (gBehind) gBehind.textContent = data.car_behind && data.car_behind.gap_s !== null ? `+${data.car_behind.gap_s.toFixed(2)}s` : '--';
    const gLast = document.getElementById('g-last');
    if (gLast) gLast.textContent = formatLapTime(data.last_lap_s);
    const gFuel = document.getElementById('g-fuel');
    if (gFuel && data.fuel && data.fuel.laps_delta !== null) {
      gFuel.textContent = `${data.fuel.laps_delta >= 0 ? '+' : ''}${data.fuel.laps_delta.toFixed(1)}`;
      gFuel.className = `big ${data.fuel.laps_delta >= 0 ? 'up' : 'warn'}`;
    }

    // Modern Tyres
    if (data.tyres) {
      const t = data.tyres;
      const wear = t.wear_pct || {};
      const temps = t.surface_temp_c || {};

      const flW = wear['front left'] || 0;
      const frW = wear['front right'] || 0;
      const rlW = wear['rear left'] || 0;
      const rrW = wear['rear right'] || 0;

      const setTile = (prefix, w, temp) => {
        const wEl = document.getElementById(`${prefix}-wear`);
        const bEl = document.getElementById(`${prefix}-bar`);
        const tEl = document.getElementById(`${prefix}-temp`);
        if (wEl) wEl.textContent = `${Math.round(w)}%`;
        if (bEl) {
          bEl.style.width = `${Math.min(100, Math.round(w))}%`;
          bEl.style.background = w > 50 ? 'var(--red)' : w > 35 ? 'var(--yellow)' : 'var(--green)';
        }
        if (tEl && temp) {
          const st = temp > 115 ? 'hot' : temp < 85 ? 'cold' : 'ok';
          tEl.className = `t ${st}`;
          tEl.innerHTML = `<i></i><span>${Math.round(temp)}°C${st === 'hot' ? ' · hot' : ''}</span>`;
        }
      };

      setTile('fl', flW, temps['front left']);
      setTile('fr', frW, temps['front right']);
      setTile('rl', rlW, temps['rear left']);
      setTile('rr', rrW, temps['rear right']);

      const gTyre = document.getElementById('g-tyre');
      if (gTyre) {
        const worst = Math.round(Math.max(flW, frW, rlW, rrW));
        gTyre.textContent = `${worst}%`;
      }

      const wearChip = document.getElementById('wear-chip');
      if (wearChip) wearChip.textContent = t.wear_pct_per_lap ? `${t.wear_pct_per_lap}% / lap` : '-- / lap';

      const tyreChipText = document.getElementById('tyre-chip-text');
      const tyreChipBadge = document.getElementById('tyre-chip-badge');
      if (tyreChipText) {
        tyreChipText.textContent = `${t.compound || 'Medium'} · ${t.age_laps || 0} laps`;
      }
      if (tyreChipBadge && t.compound) {
        const cCode = t.compound[0].toUpperCase();
        tyreChipBadge.className = `tyre ${cCode}`;
      }
    }

    // Modern Fuel & ERS
    if (data.fuel) {
      const fTank = document.getElementById('f-tank');
      if (fTank) fTank.textContent = `${data.fuel.kg} kg`;
      const fMargin = document.getElementById('f-margin');
      if (fMargin && data.fuel.laps_delta !== null) {
        fMargin.textContent = `${data.fuel.laps_delta >= 0 ? '+' : ''}${data.fuel.laps_delta.toFixed(2)} laps`;
        fMargin.className = `v ${data.fuel.laps_delta >= 0 ? 'up' : 'down'}`;
      }
      const fBurn = document.getElementById('f-burn');
      if (fBurn) fBurn.textContent = data.fuel.kg_per_lap ? `${data.fuel.kg_per_lap}` : '--';
    }
    if (data.ers_pct !== null && data.ers_pct !== undefined) {
      const ersBar = document.getElementById('ers-bar');
      const ersV = document.getElementById('ers-v');
      if (ersBar) ersBar.style.width = `${Math.min(100, Math.max(0, data.ers_pct))}%`;
      if (ersV) ersV.textContent = `${data.ers_pct}%`;
    }

    // Modern Damage
    const flwVal = dmg['front left wing'] || 0;
    const frwVal = dmg['front right wing'] || 0;
    const flrVal = dmg['floor'] || 0;
    const difVal = dmg['diffuser'] || 0;
    const sdpVal = dmg['sidepod'] || 0;
    const gbxVal = dmg['gearbox'] || 0;
    const engVal = dmg['engine'] || 0;

    const setDmgEl = (id, val) => {
      const elDmg = document.getElementById(id);
      if (elDmg) {
        elDmg.textContent = `${val}%`;
        elDmg.className = val >= 10 ? 'bad' : val > 0 ? 'warn' : '';
      }
    };
    setDmgEl('dmg-flw', flwVal);
    setDmgEl('dmg-frw', frwVal);
    setDmgEl('dmg-floor', Math.max(flrVal, difVal));
    setDmgEl('dmg-side', sdpVal);
    setDmgEl('dmg-gear', gbxVal);
    setDmgEl('dmg-eng', engVal);

    const dmgChip = document.getElementById('dmg-chip');
    if (dmgChip) {
      dmgChip.textContent = hasDamage ? 'Damage detected' : 'No damage';
      dmgChip.className = `chip ${hasDamage ? 'warn' : 'ok'}`;
    }

    // Modern Strategy & Weather
    if (data.weather) {
      const valWeather = document.getElementById('val-weather');
      if (valWeather) valWeather.textContent = (data.weather.now || 'Clear').toUpperCase();
      const valTrackTemp = document.getElementById('val-track-temp');
      if (valTrackTemp) valTrackTemp.textContent = `Track ${data.weather.track_c || '--'}°C · Air ${data.weather.air_c || '--'}°C`;
      const valRainProb = document.getElementById('val-rain-prob');
      const valRainForecast = document.getElementById('val-rain-forecast');
      if (data.weather.next_rain) {
        if (valRainProb) valRainProb.textContent = `Rain in ${data.weather.next_rain.in_min}m`;
        if (valRainForecast) valRainForecast.textContent = `${data.weather.next_rain.pct}% probability`;
      } else {
        if (valRainProb) valRainProb.textContent = 'None expected';
        if (valRainForecast) valRainForecast.textContent = 'Clear skies';
      }
    }
    if (data.pit_window) {
      const valPitWindow = document.getElementById('val-pit-window');
      if (valPitWindow) valPitWindow.textContent = `Laps ${data.pit_window.ideal_lap}–${data.pit_window.latest_lap}`;
    }

    // Modern Center Gauges
    const cSpeed = document.getElementById('c-speed');
    if (cSpeed && data.speed_kph !== undefined && data.speed_kph !== null) {
      cSpeed.textContent = Math.round(data.speed_kph);
    }
    const cGear = document.getElementById('c-gear');
    if (cGear && data.gear !== undefined && data.gear !== null) {
      cGear.textContent = data.gear === 0 ? 'N' : data.gear === -1 ? 'R' : data.gear;
    }
    const cDrs = document.getElementById('c-drs');
    if (cDrs) {
      cDrs.textContent = data.drs ? 'DRS open' : 'DRS';
      cDrs.className = `chip ${data.drs ? 'ok' : ''}`;
    }
    if (data.throttle !== undefined) {
      const thrPct = Math.round(data.throttle * 100);
      const cThr = document.getElementById('c-thr');
      const cThrV = document.getElementById('c-thr-v');
      if (cThr) cThr.style.width = `${thrPct}%`;
      if (cThrV) cThrV.textContent = thrPct;
    }
    if (data.brake !== undefined) {
      const brkPct = Math.round(data.brake * 100);
      const cBrk = document.getElementById('c-brk');
      const cBrkV = document.getElementById('c-brk-v');
      if (cBrk) cBrk.style.width = `${brkPct}%`;
      if (cBrkV) cBrkV.textContent = brkPct;
    }
    if (data.g_force) {
      const lat = data.g_force.lat || 0;
      const lon = data.g_force.lon || 0;
      const ggDot = document.getElementById('gg-dot');
      if (ggDot) {
        ggDot.setAttribute('cx', (lat / 5 * 46).toFixed(1));
        ggDot.setAttribute('cy', (-lon / 5 * 46).toFixed(1));
      }
      const gLat = document.getElementById('g-lat');
      const gLon = document.getElementById('g-lon');
      if (gLat) gLat.textContent = `${Math.abs(lat).toFixed(1)} g`;
      if (gLon) gLon.textContent = `${(lon < 0 ? '−' : '')}${Math.abs(lon).toFixed(1)} g`;
    }
  }

  // --- Track Map Network API ---
  async function fetchTrackMap() {
    if (isFetchingOutline) return;
    isFetchingOutline = true;
    try {
      const resp = await fetch(`/api/track_map${window.location.search}`);
      if (resp.ok) {
        const data = await resp.json();
        if (data.has_outline && data.points && data.points.length > 0) {
          cachedOutline = data;
          el.mapStatusBadge.className = 'meta-pill pill-ok';
          el.mapStatusBadge.textContent = 'CIRCUIT MAPPED';
          el.mapPlaceholderHint.classList.add('hidden');
        } else {
          cachedOutline = null;
          el.mapStatusBadge.className = 'meta-pill';
          el.mapStatusBadge.textContent = 'BLANK CANVAS (MAPPING)';
          el.mapPlaceholderHint.classList.remove('hidden');
        }
      }
    } catch (e) {
      console.warn('Failed to fetch track outline:', e);
    } finally {
      isFetchingOutline = false;
    }
  }

  async function rebuildTrackMap() {
    try {
      const resp = await fetch(`/api/track_map/rebuild${window.location.search}`, { method: 'POST' });
      if (resp.ok) {
        cachedOutline = null;
        runningBounds = { minX: Infinity, maxX: -Infinity, minZ: Infinity, maxZ: -Infinity };
        el.mapStatusBadge.className = 'meta-pill pill-warn';
        el.mapStatusBadge.textContent = 'REBUILDING (CLEAN LAP NEEDED)';
        el.mapPlaceholderHint.classList.remove('hidden');
      }
    } catch (e) {
      console.warn('Failed to rebuild track map:', e);
    }
  }

  // --- Render Track Map Canvas ---
  function renderTrackMap(cars) {
    if (!el.trackMapCanvas) return;
    const canvas = el.trackMapCanvas;
    const parent = canvas.parentElement;
    const rect = parent.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;

    const w = rect.width || 400;
    const h = rect.height || 260;

    if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
      canvas.width = Math.round(w * dpr);
      canvas.height = Math.round(h * dpr);
    }

    const ctx = canvas.getContext('2d');
    ctx.resetTransform();
    ctx.scale(dpr, dpr);
    ctx.clearRect(0, 0, w, h);

    // Update running bounds for cars seen so far
    if (cars && cars.length > 0) {
      cars.forEach(c => {
        if (c.x < runningBounds.minX) runningBounds.minX = c.x;
        if (c.x > runningBounds.maxX) runningBounds.maxX = c.x;
        if (c.z < runningBounds.minZ) runningBounds.minZ = c.z;
        if (c.z > runningBounds.maxZ) runningBounds.maxZ = c.z;
      });
    }

    // Determine coordinate bounds
    let minX, maxX, minZ, maxZ;
    if (cachedOutline && cachedOutline.bounds) {
      minX = cachedOutline.bounds.min_x;
      maxX = cachedOutline.bounds.max_x;
      minZ = cachedOutline.bounds.min_z;
      maxZ = cachedOutline.bounds.max_z;
    } else if (isFinite(runningBounds.minX) && runningBounds.minX !== runningBounds.maxX) {
      minX = runningBounds.minX;
      maxX = runningBounds.maxX;
      minZ = runningBounds.minZ;
      maxZ = runningBounds.maxZ;
    } else {
      minX = -100; maxX = 100; minZ = -100; maxZ = 100;
    }

    const spanX = Math.max(maxX - minX, 10);
    const spanZ = Math.max(maxZ - minZ, 10);
    const padding = 24;
    const scale = Math.min((w - 2 * padding) / spanX, (h - 2 * padding) / spanZ);
    const offsetX = padding + (w - 2 * padding - spanX * scale) / 2;
    const offsetY = padding + (h - 2 * padding - spanZ * scale) / 2;

    function toCanvas(x, z) {
      return {
        x: offsetX + (x - minX) * scale,
        y: offsetY + (z - minZ) * scale,
      };
    }

    // 1. Draw Background Grid (Subtle radar)
    ctx.strokeStyle = 'rgba(255, 255, 255, 0.04)';
    ctx.lineWidth = 1;
    const gridSpacing = 40;
    for (let gx = 0; gx < w; gx += gridSpacing) {
      ctx.beginPath(); ctx.moveTo(gx, 0); ctx.lineTo(gx, h); ctx.stroke();
    }
    for (let gy = 0; gy < h; gy += gridSpacing) {
      ctx.beginPath(); ctx.moveTo(0, gy); ctx.lineTo(w, gy); ctx.stroke();
    }

    // 2. Draw Circuit Outline (If available)
    if (cachedOutline && cachedOutline.points && cachedOutline.points.length > 1) {
      const pts = cachedOutline.points;

      // Base Track Ribbon
      ctx.beginPath();
      const first = toCanvas(pts[0][0], pts[0][1]);
      ctx.moveTo(first.x, first.y);
      for (let i = 1; i < pts.length; i++) {
        const p = toCanvas(pts[i][0], pts[i][1]);
        ctx.lineTo(p.x, p.y);
      }
      ctx.closePath();
      ctx.strokeStyle = 'rgba(255, 255, 255, 0.15)';
      ctx.lineWidth = 6;
      ctx.lineCap = 'round';
      ctx.lineJoin = 'round';
      ctx.stroke();

      // Glowing Centerline
      ctx.beginPath();
      ctx.moveTo(first.x, first.y);
      for (let i = 1; i < pts.length; i++) {
        const p = toCanvas(pts[i][0], pts[i][1]);
        ctx.lineTo(p.x, p.y);
      }
      ctx.closePath();
      ctx.strokeStyle = '#00e5ff';
      ctx.lineWidth = 2;
      ctx.shadowColor = 'rgba(0, 229, 255, 0.6)';
      ctx.shadowBlur = 6;
      ctx.stroke();
      ctx.shadowBlur = 0;

      // Sector Markers
      if (cachedOutline.sectors) {
        if (cachedOutline.sectors.s2) {
          const s2 = toCanvas(cachedOutline.sectors.s2[0], cachedOutline.sectors.s2[1]);
          drawSectorMarker(ctx, s2, '#ffd600', 'S2');
        }
        if (cachedOutline.sectors.s3) {
          const s3 = toCanvas(cachedOutline.sectors.s3[0], cachedOutline.sectors.s3[1]);
          drawSectorMarker(ctx, s3, '#d500f9', 'S3');
        }
      }

      // Start/Finish Line
      if (cachedOutline.finish_line) {
        const fin = toCanvas(cachedOutline.finish_line[0], cachedOutline.finish_line[1]);
        drawFinishLine(ctx, fin);
      }
    }

    // 3. Draw All Cars
    if (cars && cars.length > 0) {
      const sorted = [...cars].sort((a, b) => {
        if (a.is_player) return 1;
        if (b.is_player) return -1;
        if (a.is_ahead || a.is_behind) return 1;
        if (b.is_ahead || b.is_behind) return -1;
        return 0;
      });

      sorted.forEach(c => {
        const pt = toCanvas(c.x, c.z);
        if (c.is_player) {
          // Player car: Large pulsing cyan dot with outer halo
          ctx.beginPath();
          ctx.arc(pt.x, pt.y, 11, 0, Math.PI * 2);
          ctx.fillStyle = 'rgba(0, 229, 255, 0.25)';
          ctx.fill();

          ctx.beginPath();
          ctx.arc(pt.x, pt.y, 6.5, 0, Math.PI * 2);
          ctx.fillStyle = '#00e5ff';
          ctx.strokeStyle = '#ffffff';
          ctx.lineWidth = 1.5;
          ctx.fill();
          ctx.stroke();

          // Label
          ctx.fillStyle = '#00e5ff';
          ctx.font = 'bold 10px monospace';
          ctx.textAlign = 'center';
          ctx.fillText(`P${c.position}`, pt.x, pt.y - 10);
        } else if (c.is_ahead || c.is_behind) {
          // Rival car (Ahead / Behind): Amber
          ctx.beginPath();
          ctx.arc(pt.x, pt.y, 5.5, 0, Math.PI * 2);
          ctx.fillStyle = '#ffab00';
          ctx.strokeStyle = '#000000';
          ctx.lineWidth = 1;
          ctx.fill();
          ctx.stroke();

          ctx.fillStyle = '#ffab00';
          ctx.font = 'bold 9px monospace';
          ctx.textAlign = 'center';
          ctx.fillText(`${c.position}`, pt.x, pt.y - 8);
        } else if (c.pitting) {
          // Pitting car: Grey
          ctx.beginPath();
          ctx.arc(pt.x, pt.y, 4, 0, Math.PI * 2);
          ctx.fillStyle = '#555555';
          ctx.strokeStyle = '#222222';
          ctx.lineWidth = 1;
          ctx.fill();
          ctx.stroke();

          ctx.fillStyle = '#888888';
          ctx.font = '8px monospace';
          ctx.textAlign = 'center';
          ctx.fillText('PIT', pt.x, pt.y - 6);
        } else {
          // Other active cars: White
          ctx.beginPath();
          ctx.arc(pt.x, pt.y, 4.5, 0, Math.PI * 2);
          ctx.fillStyle = '#f0f4f8';
          ctx.strokeStyle = '#111520';
          ctx.lineWidth = 1;
          ctx.fill();
          ctx.stroke();

          ctx.fillStyle = '#cbd5e1';
          ctx.font = '8px monospace';
          ctx.textAlign = 'center';
          ctx.fillText(`${c.position}`, pt.x, pt.y - 6);
        }
      });
    }
  }

  function drawSectorMarker(ctx, pt, color, label) {
    ctx.beginPath();
    ctx.arc(pt.x, pt.y, 4, 0, Math.PI * 2);
    ctx.fillStyle = color;
    ctx.fill();

    ctx.fillStyle = color;
    ctx.font = 'bold 9px monospace';
    ctx.textAlign = 'center';
    ctx.fillText(label, pt.x, pt.y + 12);
  }

  function drawFinishLine(ctx, pt) {
    ctx.beginPath();
    ctx.arc(pt.x, pt.y, 5, 0, Math.PI * 2);
    ctx.fillStyle = '#ffffff';
    ctx.strokeStyle = '#ff1744';
    ctx.lineWidth = 2;
    ctx.fill();
    ctx.stroke();

    ctx.fillStyle = '#ffffff';
    ctx.font = 'bold 9px monospace';
    ctx.textAlign = 'center';
    ctx.fillText('FINISH', pt.x, pt.y + 14);
  }

  // --- Render G-Force Friction Circle ---
  function renderGForce(g) {
    if (!el.gForceCanvas || !g) return;
    const canvas = el.gForceCanvas;
    const dpr = window.devicePixelRatio || 1;
    const size = 110;

    if (canvas.width !== size * dpr || canvas.height !== size * dpr) {
      canvas.width = size * dpr;
      canvas.height = size * dpr;
    }

    const ctx = canvas.getContext('2d');
    ctx.resetTransform();
    ctx.scale(dpr, dpr);
    ctx.clearRect(0, 0, size, size);

    const cx = size / 2;
    const cy = size / 2;
    const maxG = 4.0;
    const pxPerG = (size / 2 - 8) / maxG;

    // Background circle
    ctx.fillStyle = 'rgba(0, 0, 0, 0.4)';
    ctx.beginPath();
    ctx.arc(cx, cy, size / 2 - 2, 0, Math.PI * 2);
    ctx.fill();

    // Concentric rings (1G, 2G, 3G)
    ctx.lineWidth = 1;
    [1, 2, 3].forEach(ringG => {
      ctx.beginPath();
      ctx.arc(cx, cy, ringG * pxPerG, 0, Math.PI * 2);
      ctx.strokeStyle = ringG === 3 ? 'rgba(255, 61, 0, 0.3)' : 'rgba(255, 255, 255, 0.1)';
      ctx.stroke();
    });

    // Crosshair axes
    ctx.strokeStyle = 'rgba(255, 255, 255, 0.15)';
    ctx.beginPath();
    ctx.moveTo(cx, 4); ctx.lineTo(cx, size - 4);
    ctx.moveTo(4, cy); ctx.lineTo(size - 4, cy);
    ctx.stroke();

    // Store G-history trail
    gHistory.push([g.lat, g.lon]);
    if (gHistory.length > 15) gHistory.shift();

    // Draw fading trail
    gHistory.forEach((pt, idx) => {
      const alpha = (idx + 1) / gHistory.length * 0.4;
      const tx = cx + pt[0] * pxPerG;
      const ty = cy - pt[1] * pxPerG;
      ctx.beginPath();
      ctx.arc(tx, ty, 2, 0, Math.PI * 2);
      ctx.fillStyle = `rgba(0, 229, 255, ${alpha})`;
      ctx.fill();
    });

    // Current G-Force Dot
    const currX = Math.min(Math.max(cx + g.lat * pxPerG, 6), size - 6);
    const currY = Math.min(Math.max(cy - g.lon * pxPerG, 6), size - 6);
    const totalG = Math.sqrt(g.lat * g.lat + g.lon * g.lon);

    let dotColor = '#00e676';
    if (totalG > 2.8) dotColor = '#ff3d00';
    else if (totalG > 1.8) dotColor = '#ffd600';

    ctx.beginPath();
    ctx.arc(currX, currY, 4.5, 0, Math.PI * 2);
    ctx.fillStyle = dotColor;
    ctx.strokeStyle = '#ffffff';
    ctx.lineWidth = 1.5;
    ctx.shadowColor = dotColor;
    ctx.shadowBlur = 8;
    ctx.fill();
    ctx.stroke();
    ctx.shadowBlur = 0;

    // Update readout labels
    if (el.valGLat) el.valGLat.textContent = `${g.lat >= 0 ? '+' : ''}${g.lat.toFixed(1)}G`;
    if (el.valGLon) el.valGLon.textContent = `${g.lon >= 0 ? '+' : ''}${g.lon.toFixed(1)}G`;
  }

  // --- UI Update: Render Timing Tower Table ---
  function renderTimingTower(cars) {
    if (el.towerCount) el.towerCount.textContent = `${cars.length} CARS`;
    const modernTower = document.getElementById('tower');
    const carsChip = document.getElementById('cars-chip');
    if (carsChip) carsChip.textContent = `${cars.length} cars`;

    if (!cars.length) {
      if (el.towerTbody) el.towerTbody.innerHTML = '<tr class="empty-row"><td colspan="9">No active cars in session</td></tr>';
      if (modernTower) {
        modernTower.innerHTML = '<tr><td colspan="8"><div class="empty"><strong>No cars in session</strong><span class="help">The order appears when the session starts.</span></div></td></tr>';
      }
      return;
    }

    if (el.towerTbody) {
      const rows = cars.map(c => {
        const isPlayer = c.is_player ? 'player-row' : '';
        const compoundCode = (c.tyre_compound || 'm')[0].toLowerCase();
        const badgeClass = `badge-${compoundCode}`;
        const gapAhead = c.position === 1 ? 'LEADER' : c.gap_ahead !== null ? `+${c.gap_ahead}s` : '--';
        const gapLeader = c.position === 1 ? '--' : c.gap_leader !== null ? `+${c.gap_leader}s` : '--';
        const pen = c.penalties > 0 ? `<span style="color: var(--color-red)">+${c.penalties}s</span>` : '0';

        return `
          <tr class="${isPlayer}">
            <td class="td-pos mono">P${c.position}</td>
            <td class="td-driver">${c.name || 'Car ' + c.car_idx}</td>
            <td class="mono">${gapAhead}</td>
            <td class="mono">${gapLeader}</td>
            <td class="mono">${formatLapTime(c.last_lap)}</td>
            <td><span class="${badgeClass}">${(c.tyre_compound || '?').toUpperCase()}</span></td>
            <td class="mono">${c.tyre_age !== null ? c.tyre_age : '--'}</td>
            <td class="mono">${c.pit_stops !== null ? c.pit_stops : 0}</td>
            <td class="mono">${pen}</td>
          </tr>
        `;
      }).join('');
      el.towerTbody.innerHTML = rows;
    }

    if (modernTower) {
      let prev = 0;
      modernTower.innerHTML = cars.map((c, i) => {
        const isPlayer = !!c.is_player;
        const gap = c.gap_leader !== null ? c.gap_leader : 0;
        const interval = i === 0 ? 'Leader' : c.is_in_pit ? '—' : '+' + (c.gap_ahead !== null ? c.gap_ahead : (gap - prev).toFixed(3));
        const lead = i === 0 ? '—' : c.is_in_pit ? 'In pit' : '+' + (c.gap_leader !== null ? c.gap_leader : gap.toFixed(3));
        if (!c.is_in_pit) prev = gap;
        const cmp = (c.tyre_compound || 'M')[0].toUpperCase();
        const age = c.tyre_age !== null ? c.tyre_age : 0;
        const pen = c.penalties > 0 ? `+${c.penalties}s` : '';
        return `<tr class="${isPlayer ? 'me' : ''}">
          <td class="r"><b>${c.position}</b></td>
          <td><b>${c.name || (isPlayer ? 'You' : 'Car ' + c.car_idx)}</b> <span class="dim">${c.team || (isPlayer ? 'Player' : '')}</span></td>
          <td class="r">${interval}</td>
          <td class="r muted">${lead}</td>
          <td class="r">${formatLapTime(c.last_lap)}</td>
          <td><span class="tyre ${cmp}" aria-label="${cmp}">${cmp}</span> <span class="muted">${age}</span></td>
          <td class="r">${c.pit_stops !== null ? c.pit_stops : 0}</td>
          <td class="r ${pen ? 'warn' : 'dim'}">${pen || '—'}</td>
        </tr>`;
      }).join('');
    }
  }

  // --- UI Update: Radio Message Bubble ---
  function addRadioMessage(msg) {
    const isDriver = (msg.sender || '').toLowerCase() === 'driver';
    const isSpotter = (msg.sender || '').toLowerCase() === 'spotter';
    const timeStr = new Date(msg.timestamp * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    const latencyBadge = msg.latency_ms ? `<span class="latency-badge mono">${Math.round(msg.latency_ms)}ms</span>` : '';
    const metaText = msg.latency_ms ? `AI · ${(msg.latency_ms / 1000).toFixed(1)}s` : '';

    if (el.radioLog) {
      const bubble = document.createElement('div');
      bubble.className = `radio-bubble ${isDriver ? 'driver' : 'engineer'}`;
      bubble.innerHTML = `
        <div class="radio-meta">
          <span class="${isDriver ? 'sender-driver' : 'sender-engineer'}">[${(msg.sender || '').toUpperCase()}]</span>
          <div>${latencyBadge} <span class="mono">${timeStr}</span></div>
        </div>
        <div class="radio-text">"${msg.text}"</div>
      `;
      const welcome = el.radioLog.querySelector('.radio-welcome-notice');
      if (welcome) welcome.remove();
      el.radioLog.appendChild(bubble);
      el.radioLog.scrollTop = el.radioLog.scrollHeight;
    }

    const modernFeed = document.getElementById('feed');
    if (modernFeed) {
      const roleClass = isDriver ? 'you' : isSpotter ? 'spot' : 'eng';
      const senderName = isDriver ? 'You' : isSpotter ? 'Spotter' : 'Engineer';
      const wrap = document.createElement('div');
      wrap.className = `msg ${roleClass} enter`;
      wrap.innerHTML = `
        <div class="who"><b>${senderName}</b><time>${timeStr}</time></div>
        <p>${msg.text}</p>
        ${metaText ? `<span class="meta">${metaText}</span>` : ''}
      `;
      const empty = modernFeed.querySelector('.empty');
      if (empty) empty.remove();
      modernFeed.appendChild(wrap);
      if (modernFeed.children.length > 40) modernFeed.firstChild.remove();
      requestAnimationFrame(() => requestAnimationFrame(() => wrap.classList.add('in')));
      modernFeed.scrollTop = modernFeed.scrollHeight;
    }

    const gRadio = document.getElementById('g-radio');
    if (gRadio) gRadio.textContent = msg.text;
  }

  // --- WebSocket Connection with Exponential Backoff ---
  function connectWebSocket() {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const token = getToken();
    const wsUrl = `${protocol}//${window.location.host}/ws/live${token ? '?token=' + encodeURIComponent(token) : ''}`;

    el.connectionBadge.className = 'status-pill connecting';
    el.connectionText.textContent = 'CONNECTING...';

    if (socket) {
      try { socket.close(); } catch (e) {}
      socket = null;
    }

    try {
      socket = new WebSocket(wsUrl);
    } catch (err) {
      console.error('[Dashboard] WebSocket creation error:', err);
      scheduleReconnect();
      return;
    }

    let lastLiveLoggedState = null;
    let lastConsoleLogTime = 0;

    socket.onopen = function () {
      reconnectAttempts = 0;
      console.log('[Dashboard] WebSocket connected to Race Engineer server:', wsUrl);
      console.log('[Dashboard] Waiting for F1 25 game telemetry stream from Python...');
    };

    socket.onmessage = function (event) {
      try {
        const payload = JSON.parse(event.data);
        if (payload.type === 'radio') {
          console.log('[Dashboard Radio Message]', payload);
          addRadioMessage(payload);
        } else if (payload.type === 'initial_state') {
          console.log('[Dashboard Initial State]', payload);
          if (payload.radio_history && Array.isArray(payload.radio_history)) {
            payload.radio_history.forEach(m => addRadioMessage(m));
          }
          if (payload.overtakes && Array.isArray(payload.overtakes)) {
            renderOvertakes(payload.overtakes);
          }
          if (payload.live) {
            updateTelemetryUI(payload.live);
          }
        } else if (payload.type === 'overtake') {
          appendOvertakeRow(payload.data || payload);
        } else {
          const isLive = !!payload.session_live;
          const now = Date.now();
          if (isLive !== lastLiveLoggedState || (now - lastConsoleLogTime > 2500)) {
            lastConsoleLogTime = now;
            lastLiveLoggedState = isLive;
            console.log(`[Dashboard Telemetry] Server: Connected | F1 Game: ${isLive ? 'STREAMING (LIVE)' : 'STANDBY (NO GAME DATA YET)'}`, {
              f1_game_streaming: isLive,
              track: payload.track || '(waiting for session)',
              speed_kph: payload.speed_kph,
              gear: payload.gear,
              lap: payload.lap,
              position: payload.position,
              telemetry_age_s: payload.telemetry_age_s
            });
          }
          updateTelemetryUI(payload);
        }
      } catch (err) {
        console.error('[Dashboard] Error parsing WebSocket frame:', err, event.data);
      }
    };

    socket.onclose = function (event) {
      el.connectionBadge.className = 'status-pill offline';
      el.connectionText.textContent = 'OFFLINE';
      console.warn('[Dashboard] WebSocket closed:', event.code, event.reason);
      scheduleReconnect();
    };

    socket.onerror = function (error) {
      console.error('[Dashboard] WebSocket error:', error);
      socket.close();
    };
  }

  function scheduleReconnect() {
    if (reconnectTimer) clearTimeout(reconnectTimer);
    reconnectAttempts++;
    // Exponential backoff: 500ms, 1s, 2s, 4s, capped at 5s
    const delay = Math.min(500 * Math.pow(2, reconnectAttempts - 1), 5000);
    el.connectionText.textContent = `RETRY IN ${Math.round(delay / 1000)}s`;
    reconnectTimer = setTimeout(connectWebSocket, delay);
  }

  // ==========================================================================
  // LAPS & TELEMETRY ANALYSIS CLIENT LOGIC
  // ==========================================================================
  let loadedSessions = [];
  let currentSessionLaps = [];
  let currentStints = [];

  function initTabs() {
    if (!el.tabBtnLive || !el.tabBtnLaps) return;

    function switchTab(activeBtn, activeView) {
      const tabs = [el.tabBtnLive, el.tabBtnLaps, el.tabBtnSetups, el.tabBtnTracks, el.tabBtnUsage, el.tabBtnSettings];
      const views = [el.viewLive, el.viewLaps, el.viewSetups, el.viewTracks, el.viewUsage, el.viewSettings];
      tabs.forEach(t => { 
        if (t) {
          t.classList.toggle('active', t === activeBtn);
          if (t === activeBtn) t.setAttribute('aria-current', 'page');
          else t.removeAttribute('aria-current');
        }
      });
      views.forEach(v => { 
        if (v) {
          v.classList.toggle('active', v === activeView);
          v.classList.toggle('hidden', v !== activeView);
        }
      });
    }

    function activateSettingsSubnav(secKey) {
      document.querySelectorAll('.subnav a').forEach(a => {
        const href = a.getAttribute('href') || '';
        a.classList.toggle('active', href.includes(secKey));
      });
      const sec = document.getElementById(`sec-${secKey}`);
      if (sec) sec.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }

    document.querySelectorAll('.subnav a').forEach(a => {
      a.addEventListener('click', () => {
        const href = a.getAttribute('href') || '';
        const key = href.replace('#settings-', '').replace('#sec-', '');
        activateSettingsSubnav(key);
      });
    });

    function handleHashRoute() {
      const raw = (window.location.hash || '#live').slice(1).toLowerCase();
      const parts = raw.split(':');
      const clean = parts[0];
      let viewKey = 'live';
      let subSec = null;

      if (clean.startsWith('settings')) {
        viewKey = 'settings';
        if (clean.includes('-')) {
          subSec = clean.split('-')[1];
        }
      } else if (clean === 'laps') {
        viewKey = 'laps';
      } else if (clean === 'setups') {
        viewKey = 'setups';
      } else if (clean === 'tracks') {
        viewKey = 'tracks';
      } else if (clean === 'usage') {
        viewKey = 'usage';
      } else {
        viewKey = 'live';
      }

      const map = {
        live: { btn: el.tabBtnLive, view: el.viewLive },
        laps: { btn: el.tabBtnLaps, view: el.viewLaps },
        setups: { btn: el.tabBtnSetups, view: el.viewSetups },
        tracks: { btn: el.tabBtnTracks, view: el.viewTracks },
        usage: { btn: el.tabBtnUsage, view: el.viewUsage },
        settings: { btn: el.tabBtnSettings, view: el.viewSettings },
      };

      const target = map[viewKey];
      if (target && target.btn && target.view) {
        switchTab(target.btn, target.view);
        if (viewKey === 'laps') {
          initMockCharts();
          if (loadedSessions.length === 0) fetchSessions();
        } else if (viewKey === 'setups') {
          fetchSetups();
          fetchCurrentSetup();
        } else if (viewKey === 'tracks') {
          fetchTracksList();
        } else if (viewKey === 'usage') {
          fetchUsageStatus();
          fetchUsageHistory();
        } else if (viewKey === 'settings') {
          fetchPersonaSettings();
          fetchAudioDevices();
          fetchVolumes();
          fetchPortSettings();
          if (subSec) {
            setTimeout(() => activateSettingsSubnav(subSec), 100);
          }
        }
      }
    }

    window.addEventListener('hashchange', handleHashRoute);

    // Popover handler
    const linkChip = document.getElementById('link-chip');
    const linkPop = document.getElementById('link-pop');
    if (linkChip && linkPop) {
      function setPop(open) {
        linkPop.classList.toggle('open', open);
        linkChip.setAttribute('aria-expanded', open ? 'true' : 'false');
      }
      linkChip.addEventListener('click', (e) => {
        e.stopPropagation();
        setPop(!linkPop.classList.contains('open'));
      });
      document.addEventListener('click', (e) => {
        if (!linkPop.contains(e.target) && e.target !== linkChip) setPop(false);
      });
      document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && linkPop.classList.contains('open')) {
          setPop(false);
          linkChip.focus();
        }
      });
    }

    // Layout Switcher (Full vs Glance)
    const btnFull = document.getElementById('btn-layout-full');
    const btnGlance = document.getElementById('btn-layout-glance');
    const viewLive = document.getElementById('view-live');
    if (btnFull && btnGlance && viewLive) {
      btnFull.addEventListener('click', () => {
        btnFull.setAttribute('aria-pressed', 'true');
        btnGlance.setAttribute('aria-pressed', 'false');
        viewLive.classList.remove('is-glance');
      });
      btnGlance.addEventListener('click', () => {
        btnFull.setAttribute('aria-pressed', 'false');
        btnGlance.setAttribute('aria-pressed', 'true');
        viewLive.classList.add('is-glance');
      });
    }

    // Volume Sliders Live Readout
    ['v-eng', 'v-spot', 'v-beep'].forEach(id => {
      const slider = document.getElementById(id);
      const out = document.getElementById(`${id}-out`);
      if (slider && out) {
        slider.addEventListener('input', () => {
          out.textContent = slider.value + '%';
        });
      }
    });

    // 24-Bar VU Meter Initialization
    const levelEl = document.getElementById('level');
    if (levelEl && levelEl.children.length === 0) {
      levelEl.innerHTML = Array.from({ length: 24 }, (_, i) => `<i class="${i > 18 ? 'hi' : ''}"></i>`).join('');
    }

    el.tabBtnLive.addEventListener('click', () => {
      switchTab(el.tabBtnLive, el.viewLive);
    });

    el.tabBtnLaps.addEventListener('click', () => {
      switchTab(el.tabBtnLaps, el.viewLaps);
      initMockCharts();
      if (loadedSessions.length === 0) {
        fetchSessions();
      }
    });

    if (el.tabBtnSetups) {
      el.tabBtnSetups.addEventListener('click', () => {
        switchTab(el.tabBtnSetups, el.viewSetups);
        fetchSetups();
        fetchCurrentSetup();
      });
    }

    if (el.tabBtnTracks) {
      el.tabBtnTracks.addEventListener('click', () => {
        switchTab(el.tabBtnTracks, el.viewTracks);
        fetchTracksList();
      });
    }

    if (el.cloudQuotaPill) {
      el.cloudQuotaPill.addEventListener('click', () => {
        switchTab(el.tabBtnUsage, el.viewUsage);
        fetchUsageStatus();
        fetchUsageHistory();
      });
    }

    if (el.tabBtnUsage) {
      el.tabBtnUsage.addEventListener('click', () => {
        switchTab(el.tabBtnUsage, el.viewUsage);
        fetchUsageStatus();
        fetchUsageHistory();
      });
    }

    if (el.tabBtnSettings) {
      el.tabBtnSettings.addEventListener('click', () => {
        switchTab(el.tabBtnSettings, el.viewSettings);
        fetchPersonaSettings();
        fetchAudioDevices();
        fetchVolumes();
        fetchPortSettings();
      });
    }

    if (el.btnRefreshUsage) {
      el.btnRefreshUsage.addEventListener('click', () => {
        fetchUsageStatus();
        fetchUsageHistory();
      });
    }

    if (el.usageHistoryRange) {
      el.usageHistoryRange.addEventListener('change', () => {
        fetchUsageHistory(el.usageHistoryRange.value);
      });
    }

    if (el.btnSaveSettings) {
      el.btnSaveSettings.addEventListener('click', savePersonaSettings);
    }
    const btnDiscardEng = document.getElementById('btn-discard-engineer');
    if (btnDiscardEng) {
      btnDiscardEng.addEventListener('click', fetchPersonaSettings);
    }

    if (el.settingPersonaName) {
      el.settingPersonaName.addEventListener('change', savePersonaSettings);
    }
    if (el.settingProfanity) {
      el.settingProfanity.addEventListener('change', savePersonaSettings);
    }
    if (el.settingReactionFreq) {
      el.settingReactionFreq.addEventListener('change', savePersonaSettings);
    }
    if (el.settingMinGapS) {
      el.settingMinGapS.addEventListener('change', savePersonaSettings);
    }

    // Sound Test & Audio Hardware Event Listeners
    if (el.btnRefreshAudioDevices) {
      el.btnRefreshAudioDevices.addEventListener('click', fetchAudioDevices);
    }
    const btnSaveAud = document.getElementById('btn-save-audio');
    if (btnSaveAud) {
      btnSaveAud.addEventListener('click', async () => {
        await saveAudioDevices();
        await saveVolumes();
        btnSaveAud.textContent = 'Saved ✓';
        setTimeout(() => { if (btnSaveAud) btnSaveAud.textContent = 'Save Audio Settings'; }, 2000);
      });
    }
    if (el.selectAudioInput) {
      el.selectAudioInput.addEventListener('change', saveAudioDevices);
    }
    if (el.selectAudioOutput) {
      el.selectAudioOutput.addEventListener('change', saveAudioDevices);
    }
    if (el.sliderVolEngineer) {
      el.sliderVolEngineer.addEventListener('input', () => {
        if (el.labelVolEngineer) el.labelVolEngineer.textContent = el.sliderVolEngineer.value + '%';
      });
      el.sliderVolEngineer.addEventListener('change', saveVolumes);
    }
    if (el.sliderVolSpotter) {
      el.sliderVolSpotter.addEventListener('input', () => {
        if (el.labelVolSpotter) el.labelVolSpotter.textContent = el.sliderVolSpotter.value + '%';
      });
      el.sliderVolSpotter.addEventListener('change', saveVolumes);
    }
    if (el.sliderVolBeeps) {
      el.sliderVolBeeps.addEventListener('input', () => {
        if (el.labelVolBeeps) el.labelVolBeeps.textContent = el.sliderVolBeeps.value + '%';
      });
      el.sliderVolBeeps.addEventListener('change', saveVolumes);
    }
    if (el.btnSpeakerTest) {
      el.btnSpeakerTest.addEventListener('click', runSpeakerTest);
    }
    if (el.btnMicTest) {
      el.btnMicTest.addEventListener('pointerdown', (e) => {
        e.preventDefault();
        startMicRecording();
      });
      el.btnMicTest.addEventListener('pointerup', (e) => {
        e.preventDefault();
        stopMicRecording();
      });
      el.btnMicTest.addEventListener('mouseleave', () => {
        if (isMicRecording) stopMicRecording();
      });
    }
    if (el.btnTriggerRadioCheck) {
      el.btnTriggerRadioCheck.addEventListener('click', triggerInGameRadioCheck);
    }
    const btnRadCheckWait = document.getElementById('btn-radio-check-wait');
    if (btnRadCheckWait) {
      btnRadCheckWait.addEventListener('click', triggerInGameRadioCheck);
    }
    const btnSaveWhl = document.getElementById('btn-save-wheel');
    if (btnSaveWhl) {
      btnSaveWhl.addEventListener('click', () => {
        const val = document.getElementById('st-ptt-action')?.value || '1';
        try { localStorage.setItem('re_wheel_ptt_action', val); } catch (e) {}
        btnSaveWhl.textContent = 'Saved ✓';
        setTimeout(() => { if (btnSaveWhl) btnSaveWhl.textContent = 'Save Wheel Settings'; }, 2000);
      });
    }
    if (el.btnSavePorts) {
      el.btnSavePorts.addEventListener('click', savePortSettings);
    }

    if (el.filterTrack) {
      el.filterTrack.addEventListener('change', () => filterSessionDropdown());
    }

    if (el.filterSession) {
      el.filterSession.addEventListener('change', (e) => {
        const sessId = parseInt(e.target.value, 10);
        if (sessId) {
          fetchSessionLaps(sessId);
          fetchStints(sessId);
        }
      });
    }

    if (el.filterValid) {
      el.filterValid.addEventListener('change', () => {
        const sessId = parseInt(el.filterSession?.value, 10);
        if (sessId) {
          fetchSessionLaps(sessId);
        }
      });
    }

    if (el.btnRunCompare) {
      el.btnRunCompare.addEventListener('click', runCompareLaps);
    }

    if (el.btnRefreshSessions) {
      el.btnRefreshSessions.addEventListener('click', () => fetchSessions(true));
    }

    if (el.stintSelect) {
      el.stintSelect.addEventListener('change', (e) => {
        const stintId = parseInt(e.target.value, 10);
        renderSelectedStint(stintId);
      });
    }

    const btnRebOutline = document.getElementById('btn-rebuild-outline');
    if (btnRebOutline) {
      btnRebOutline.addEventListener('click', rebuildTrackMap);
    }

    handleHashRoute();
  }

  function initMockCharts() {
    const chartSpeedBody = document.getElementById('chart-speed-body');
    const chartInputsBody = document.getElementById('chart-inputs-body');
    const chartDeltaBody = document.getElementById('chart-delta-body');
    if (!chartSpeedBody || !chartInputsBody || !chartDeltaBody) return;
    if (chartSpeedBody.children.length > 0) return;

    const LAP_LEN = 5891;
    const CORNERS = [
      ['Abbey',1,300,288],['Farm',2,560,262],['Village',3,820,124],['The Loop',4,1010,84],['Aintree',5,1170,158],
      ['Brooklands',6,1760,118],['Luffield',7,1960,94],['Woodcote',8,2250,232],['Copse',9,2960,254],['Maggotts',10,3480,268],
      ['Becketts',11,3700,186],['Chapel',13,3900,218],['Stowe',15,4720,176],['Vale',16,5210,108],['Club',17,5470,138],
    ];
    const ADJ_A = { Stowe: -6, Luffield: -3, Club: -4, Village: 1, Copse: 2 };

    function speedAt(d, adj = {}) {
      let v = 326;
      for (const [name, , cd, vmin] of CORNERS) {
        const m = vmin + (adj[name] || 0);
        for (const off of [0, LAP_LEN, -LAP_LEN]) {
          const c = cd + off;
          const x = d - c;
          v = Math.min(v, Math.sqrt(m * m + (x < 0 ? 1040 * -x : 210 * x)));
        }
      }
      return v;
    }

    const N = 600;
    const xs = [...Array(N)].map((_, i) => i * LAP_LEN / (N - 1));
    const vA = xs.map(d => speedAt(d, ADJ_A));
    const vB = xs.map(d => speedAt(d));
    const delta = [];
    let ta = 0, tb = 0;
    for (let i = 0; i < N; i++) {
      if (i) {
        const ds = xs[i] - xs[i - 1];
        ta += ds / (vA[i] / 3.6);
        tb += ds / (vB[i] / 3.6);
      }
      delta.push(ta - tb);
    }
    const thr = vA.map((v, i) => { const s = i ? v - vA[i - 1] : 0; return s < -0.3 ? 0 : s > 0.15 || v > 324 ? 100 : 45; });
    const brk = vA.map((v, i) => { const s = i ? v - vA[i - 1] : 0; return s < -0.3 ? Math.min(100, 35 + -s * 6) : 0; });

    const W = 1000;
    const X = d => d / LAP_LEN * W;
    function lineSvg(arr, lo, hi, H) {
      return arr.map((v, i) => `${i ? 'L' : 'M'}${X(xs[i]).toFixed(1)},${(H - (v - lo) / (hi - lo) * H).toFixed(1)}`).join('');
    }
    function chartSVG(H, inner, grid) {
      return `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" style="height:${H}px"><g class="grid">${grid}</g>${inner}<line class="xh" x1="-10" x2="-10" y1="0" y2="${H}"/></svg>`;
    }

    const cg = H => CORNERS.map(c => `<line x1="${X(c[2])}" x2="${X(c[2])}" y1="0" y2="${H}"/>`).join('');
    chartSpeedBody.innerHTML = chartSVG(170,
      `<path class="series" stroke="var(--text-3)" d="${lineSvg(vB, 60, 340, 170)}"/><path class="series" stroke="var(--accent)" d="${lineSvg(vA, 60, 340, 170)}"/>`, cg(170));
    chartInputsBody.innerHTML = chartSVG(90,
      `<path class="series" stroke="var(--green)" d="${lineSvg(thr, 0, 105, 90)}"/><path class="series" stroke="var(--red)" d="${lineSvg(brk, 0, 105, 90)}"/>`, cg(90));
    const dmax = Math.max(...delta.map(Math.abs)) * 1.2 || 1;
    chartDeltaBody.innerHTML = chartSVG(90,
      `<path class="series" stroke="var(--yellow)" d="${lineSvg(delta, -dmax, dmax, 90)}"/>`, cg(90) + `<line x1="0" x2="${W}" y1="45" y2="45" style="stroke:var(--line-strong)"/>`);

    const ticksEl = document.getElementById('ticks');
    if (ticksEl) {
      ticksEl.innerHTML = CORNERS.map(c => `<span style="left:${c[2] / LAP_LEN * 100}%">${c[1]}</span>`).join('');
    }

    const box = document.getElementById('charts');
    const ro = document.getElementById('readout');
    if (box && ro) {
      box.querySelectorAll('.chart svg').forEach(svg => {
        svg.addEventListener('pointermove', e => {
          const r = svg.getBoundingClientRect();
          const fx = Math.min(1, Math.max(0, (e.clientX - r.left) / r.width));
          const i = Math.round(fx * (N - 1));
          box.querySelectorAll('.chart .xh').forEach(l => {
            l.setAttribute('x1', fx * W);
            l.setAttribute('x2', fx * W);
          });
          const br = box.getBoundingClientRect();
          ro.innerHTML = `${Math.round(xs[i])} m · <span style="color:var(--accent)">${Math.round(vA[i])}</span> vs ${Math.round(vB[i])} km/h · <span class="${delta[i] > 0 ? 'warn' : 'up'}">${delta[i] > 0 ? '+' : '−'}${Math.abs(delta[i]).toFixed(3)} s</span>`;
          ro.style.left = Math.min(br.width - ro.offsetWidth - 16, e.clientX - br.left + 12) + 'px';
          ro.classList.add('on');
        });
        svg.addEventListener('pointerleave', () => {
          ro.classList.remove('on');
          box.querySelectorAll('.chart .xh').forEach(l => {
            l.setAttribute('x1', -10);
            l.setAttribute('x2', -10);
          });
        });
      });
    }

    // Delta Map
    const deltaMap = document.getElementById('delta-map');
    const path = document.getElementById('track-path');
    if (deltaMap && path && path.getTotalLength) {
      const TL = path.getTotalLength();
      const pt = f => path.getPointAtLength(((f % 1) + 1) % 1 * TL);
      const segs = 160;
      let out = `<use href="#track-path" class="outline" style="stroke-width:16"/>`;
      for (let s = 0; s < segs; s++) {
        const a = pt(s / segs), b = pt((s + 1) / segs);
        const i0 = Math.floor(s / segs * (N - 1)), i1 = Math.floor((s + 1) / segs * (N - 1));
        const dd = delta[i1] - delta[i0];
        const col = Math.abs(dd) < 0.0015 ? 'var(--line-strong)' : dd > 0 ? 'var(--red)' : 'var(--green)';
        out += `<line x1="${a.x}" y1="${a.y}" x2="${b.x}" y2="${b.y}" stroke="${col}" stroke-width="6" stroke-linecap="round"/>`;
      }
      CORNERS.forEach(c => {
        const p = pt(c[2] / LAP_LEN);
        out += `<text x="${p.x + 9}" y="${p.y + 4}" font-size="11" fill="var(--text-3)">${c[1]}</text>`;
      });
      deltaMap.innerHTML = out;
    }
  }

  function fetchSessions(forceRefresh = false) {
    const token = getToken();
    const url = `/api/sessions?token=${encodeURIComponent(token)}`;

    fetch(url)
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return res.json();
      })
      .then((sessions) => {
        loadedSessions = sessions || [];
        populateTrackFilter();
        filterSessionDropdown();
      })
      .catch((err) => {
        console.warn('[Dashboard] Could not fetch sessions:', err);
      });
  }

  function populateTrackFilter() {
    if (!el.filterTrack) return;
    const tracks = new Map();
    loadedSessions.forEach((s) => {
      tracks.set(s.track_id, s.track_name || `Track ${s.track_id}`);
    });

    const currentVal = el.filterTrack.value;
    el.filterTrack.innerHTML = '<option value="">All Tracks</option>';
    tracks.forEach((name, id) => {
      const opt = document.createElement('option');
      opt.value = id;
      opt.textContent = name;
      el.filterTrack.appendChild(opt);
    });
    if (currentVal && tracks.has(parseInt(currentVal, 10))) {
      el.filterTrack.value = currentVal;
    }
  }

  function filterSessionDropdown() {
    if (!el.filterSession) return;
    const selTrack = el.filterTrack ? el.filterTrack.value : '';
    const filtered = loadedSessions.filter((s) => !selTrack || s.track_id === parseInt(selTrack, 10));

    el.filterSession.innerHTML = '';
    if (filtered.length === 0) {
      el.filterSession.innerHTML = '<option value="">No sessions found</option>';
      return;
    }

    filtered.forEach((s) => {
      const opt = document.createElement('option');
      opt.value = s.id;
      const d = new Date(s.started_at * 1000);
      const timeStr = `${d.toLocaleDateString()} ${d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`;
      opt.textContent = `[ID ${s.id}] ${s.track_name} — ${s.formula_name} (${timeStr})`;
      el.filterSession.appendChild(opt);
    });

    const firstId = filtered[0].id;
    el.filterSession.value = firstId;
    fetchSessionLaps(firstId);
    fetchStints(firstId);
  }

  function fetchSessionLaps(sessionId) {
    const token = getToken();
    const validOnly = el.filterValid ? el.filterValid.checked : true;
    const url = `/api/sessions/${sessionId}/laps?token=${encodeURIComponent(token)}&valid_only=${validOnly}`;

    fetch(url)
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return res.json();
      })
      .then((laps) => {
        currentSessionLaps = laps || [];
        populateLapSelectors();
      })
      .catch((err) => {
        console.warn('[Dashboard] Could not fetch session laps:', err);
      });
    fetchSessionOvertakes(sessionId);
  }

  function fetchSessionOvertakes(sessionId) {
    const token = getToken();
    const url = `/api/sessions/${sessionId}/overtakes?token=${encodeURIComponent(token)}`;
    fetch(url)
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return res.json();
      })
      .then((data) => {
        renderOvertakes(data.overtakes || []);
      })
      .catch((err) => {
        console.warn('[Dashboard] Could not fetch session overtakes:', err);
      });
  }

  function populateLapSelectors() {
    if (!el.selectLapA || !el.selectLapB) return;
    el.selectLapA.innerHTML = '';
    el.selectLapB.innerHTML = '';

    if (currentSessionLaps.length === 0) {
      el.selectLapA.innerHTML = '<option value="">No completed laps</option>';
      el.selectLapB.innerHTML = '<option value="">No completed laps</option>';
      return;
    }

    // Sort valid laps by time to find Personal Best
    const validLaps = currentSessionLaps.filter((l) => l.is_valid && l.lap_time_s > 0);
    const pbLap = validLaps.length > 0 ? validLaps.reduce((best, cur) => cur.lap_time_s < best.lap_time_s ? cur : best, validLaps[0]) : null;

    currentSessionLaps.forEach((l) => {
      const isPb = pbLap && pbLap.id === l.id;
      const label = `Lap ${l.lap_num}: ${formatLapTime(l.lap_time_s)}${isPb ? ' ⭐ (PB)' : ''} [${l.compound_name || 'Tyre'}]`;

      const optA = document.createElement('option');
      optA.value = l.id;
      optA.textContent = label;
      el.selectLapA.appendChild(optA);

      const optB = document.createElement('option');
      optB.value = l.id;
      optB.textContent = label;
      el.selectLapB.appendChild(optB);
    });

    // Default Lap A to latest lap, Lap B to PB or second to last
    el.selectLapA.value = currentSessionLaps[currentSessionLaps.length - 1].id;
    if (pbLap) {
      el.selectLapB.value = pbLap.id;
    } else if (currentSessionLaps.length > 1) {
      el.selectLapB.value = currentSessionLaps[0].id;
    }

    // Automatically trigger comparison
    runCompareLaps();
  }

  function runCompareLaps() {
    if (!el.selectLapA || !el.selectLapB) return;
    const lapAId = parseInt(el.selectLapA.value, 10);
    const lapBId = parseInt(el.selectLapB.value, 10);

    if (!lapAId || !lapBId) return;

    const token = getToken();
    const url = `/api/laps/compare?lap1=${lapAId}&lap2=${lapBId}&token=${encodeURIComponent(token)}`;

    fetch(url)
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return res.json();
      })
      .then((data) => {
        updateCompareSummary(data);
        renderSpeedChart(data.telemetry);
        renderInputsChart(data.telemetry);
        renderDeltaChart(data.telemetry);
        renderLapsTrackMap(data.track_points);
        renderCornerBreakdown(data.corners);
      })
      .catch((err) => {
        console.warn('[Dashboard] Could not compare laps:', err);
      });
  }

  function updateCompareSummary(data) {
    if (!data || !data.lap1 || !data.lap2) return;
    const l1 = data.lap1;
    const l2 = data.lap2;
    const delta = data.total_delta_s;

    if (el.summaryLapATime) el.summaryLapATime.textContent = formatLapTime(l1.lap_time_s);
    if (el.summaryLapAMeta) el.summaryLapAMeta.textContent = `Lap ${l1.lap_num} • Pos P${l1.position || '-'} • S1: ${l1.sector1_s ? l1.sector1_s.toFixed(3) : '-'} S2: ${l1.sector2_s ? l1.sector2_s.toFixed(3) : '-'} S3: ${l1.sector3_s ? l1.sector3_s.toFixed(3) : '-'}`;

    if (el.summaryLapBTime) el.summaryLapBTime.textContent = formatLapTime(l2.lap_time_s);
    if (el.summaryLapBMeta) el.summaryLapBMeta.textContent = `Lap ${l2.lap_num} • Ref Target • S1: ${l2.sector1_s ? l2.sector1_s.toFixed(3) : '-'} S2: ${l2.sector2_s ? l2.sector2_s.toFixed(3) : '-'} S3: ${l2.sector3_s ? l2.sector3_s.toFixed(3) : '-'}`;

    if (el.summaryDeltaBadge) {
      const sign = delta > 0 ? '+' : '';
      el.summaryDeltaBadge.textContent = `${sign}${delta.toFixed(3)}s`;
      el.summaryDeltaBadge.className = 'delta-badge ' + (delta < -0.01 ? 'faster' : delta > 0.01 ? 'slower' : 'neutral');
    }
  }

  // --- Responsive Canvas Chart Renderers ---
  function setupCanvas(canvas, height) {
    if (!canvas) return null;
    const rect = canvas.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    const w = rect.width || canvas.width || 700;
    const h = height || 180;
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
    const ctx = canvas.getContext('2d');
    ctx.resetTransform?.();
    ctx.scale(dpr, dpr);
    return { ctx, width: w, height: h };
  }

  function renderSpeedChart(tel) {
    const res = setupCanvas(el.chartSpeed, 190);
    if (!res || !tel || !tel.distance || tel.distance.length < 2) return;
    const { ctx, width: W, height: H } = res;

    ctx.clearRect(0, 0, W, H);
    const padL = 40, padR = 20, padT = 15, padB = 25;
    const plotW = W - padL - padR;
    const plotH = H - padT - padB;

    const maxDist = tel.distance[tel.distance.length - 1] || 5000;
    const maxSpd = 360;

    // Grid Lines (Speed)
    ctx.strokeStyle = 'rgba(255, 255, 255, 0.08)';
    ctx.lineWidth = 1;
    ctx.fillStyle = '#8292a8';
    ctx.font = '10px ui-monospace, SFMono-Regular, monospace';

    for (let spd = 50; spd <= 350; spd += 50) {
      const y = padT + plotH - (spd / maxSpd) * plotH;
      ctx.beginPath();
      ctx.moveTo(padL, y);
      ctx.lineTo(W - padR, y);
      ctx.stroke();
      ctx.fillText(`${spd}`, 10, y + 3);
    }

    // Distance ticks
    for (let d = 1000; d < maxDist; d += 1000) {
      const x = padL + (d / maxDist) * plotW;
      ctx.beginPath();
      ctx.moveTo(x, padT);
      ctx.lineTo(x, padT + plotH);
      ctx.stroke();
      ctx.fillText(`${(d / 1000).toFixed(0)}k`, x - 6, H - 8);
    }

    // Series 2 (Lap B Ref) - Amber
    ctx.strokeStyle = '#ffab00';
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    for (let i = 0; i < tel.distance.length; i++) {
      const x = padL + (tel.distance[i] / maxDist) * plotW;
      const y = padT + plotH - (Math.min(maxSpd, tel.speed2[i]) / maxSpd) * plotH;
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    }
    ctx.stroke();

    // Series 1 (Lap A) - Cyan Glow
    ctx.strokeStyle = '#00e5ff';
    ctx.lineWidth = 2.2;
    ctx.shadowColor = 'rgba(0, 229, 255, 0.4)';
    ctx.shadowBlur = 4;
    ctx.beginPath();
    for (let i = 0; i < tel.distance.length; i++) {
      const x = padL + (tel.distance[i] / maxDist) * plotW;
      const y = padT + plotH - (Math.min(maxSpd, tel.speed1[i]) / maxSpd) * plotH;
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    }
    ctx.stroke();
    ctx.shadowBlur = 0;
  }

  function renderInputsChart(tel) {
    const res = setupCanvas(el.chartInputs, 150);
    if (!res || !tel || !tel.distance || tel.distance.length < 2) return;
    const { ctx, width: W, height: H } = res;

    ctx.clearRect(0, 0, W, H);
    const padL = 40, padR = 20, padT = 15, padB = 25;
    const plotW = W - padL - padR;
    const plotH = H - padT - padB;
    const maxDist = tel.distance[tel.distance.length - 1] || 5000;

    // Grid lines (0%, 50%, 100%)
    ctx.strokeStyle = 'rgba(255, 255, 255, 0.08)';
    ctx.lineWidth = 1;
    ctx.fillStyle = '#8292a8';
    ctx.font = '10px ui-monospace, monospace';

    [0, 0.5, 1.0].forEach((v) => {
      const y = padT + plotH - v * plotH;
      ctx.beginPath();
      ctx.moveTo(padL, y);
      ctx.lineTo(W - padR, y);
      ctx.stroke();
      ctx.fillText(`${Math.round(v * 100)}%`, 8, y + 3);
    });

    // Helper to draw an input line
    const drawLine = (vals, color, width, isDashed = false) => {
      ctx.strokeStyle = color;
      ctx.lineWidth = width;
      if (isDashed) ctx.setLineDash([4, 4]);
      else ctx.setLineDash([]);
      ctx.beginPath();
      for (let i = 0; i < tel.distance.length; i++) {
        const x = padL + (tel.distance[i] / maxDist) * plotW;
        const y = padT + plotH - Math.max(0, Math.min(1.0, vals[i])) * plotH;
        if (i === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      }
      ctx.stroke();
      ctx.setLineDash([]);
    };

    // Draw Lap B inputs (dashed / muted)
    if (tel.throttle2) drawLine(tel.throttle2, 'rgba(0, 230, 118, 0.4)', 1.5, true);
    if (tel.brake2) drawLine(tel.brake2, 'rgba(255, 61, 0, 0.4)', 1.5, true);

    // Draw Lap A inputs (solid)
    if (tel.throttle1) drawLine(tel.throttle1, '#00e676', 2.0);
    if (tel.brake1) drawLine(tel.brake1, '#ff3d00', 2.0);
  }

  function renderDeltaChart(tel) {
    const res = setupCanvas(el.chartDelta, 150);
    if (!res || !tel || !tel.distance || tel.distance.length < 2) return;
    const { ctx, width: W, height: H } = res;

    ctx.clearRect(0, 0, W, H);
    const padL = 40, padR = 20, padT = 15, padB = 25;
    const plotW = W - padL - padR;
    const plotH = H - padT - padB;
    const maxDist = tel.distance[tel.distance.length - 1] || 5000;

    // Find min and max delta for scaling, centered around 0
    let maxAbsDelta = 0.5;
    for (let i = 0; i < tel.delta.length; i++) {
      const absD = Math.abs(tel.delta[i]);
      if (absD > maxAbsDelta) maxAbsDelta = absD;
    }
    maxAbsDelta = Math.min(Math.max(maxAbsDelta * 1.15, 0.5), 5.0);

    const zeroY = padT + plotH / 2;

    // Grid lines
    ctx.strokeStyle = 'rgba(255, 255, 255, 0.08)';
    ctx.lineWidth = 1;
    ctx.fillStyle = '#8292a8';
    ctx.font = '10px ui-monospace, monospace';

    [-maxAbsDelta, -maxAbsDelta / 2, 0, maxAbsDelta / 2, maxAbsDelta].forEach((dVal) => {
      const y = zeroY - (dVal / maxAbsDelta) * (plotH / 2);
      ctx.beginPath();
      ctx.moveTo(padL, y);
      ctx.lineTo(W - padR, y);
      ctx.stroke();
      const sign = dVal > 0 ? '+' : '';
      ctx.fillText(`${sign}${dVal.toFixed(1)}s`, 8, y + 3);
    });

    // Zero baseline
    ctx.strokeStyle = '#8292a8';
    ctx.lineWidth = 1.2;
    ctx.beginPath();
    ctx.moveTo(padL, zeroY);
    ctx.lineTo(W - padR, zeroY);
    ctx.stroke();

    // Delta curve & fill
    for (let i = 0; i < tel.distance.length - 1; i++) {
      const x1 = padL + (tel.distance[i] / maxDist) * plotW;
      const x2 = padL + (tel.distance[i + 1] / maxDist) * plotW;
      const y1 = zeroY - (tel.delta[i] / maxAbsDelta) * (plotH / 2);
      const y2 = zeroY - (tel.delta[i + 1] / maxAbsDelta) * (plotH / 2);

      const isFaster = tel.delta[i] <= 0;
      ctx.strokeStyle = isFaster ? '#00e676' : '#ff3d00';
      ctx.lineWidth = 2.2;
      ctx.beginPath();
      ctx.moveTo(x1, y1);
      ctx.lineTo(x2, y2);
      ctx.stroke();

      // Soft fill to baseline
      ctx.fillStyle = isFaster ? 'rgba(0, 230, 118, 0.15)' : 'rgba(255, 61, 0, 0.15)';
      ctx.beginPath();
      ctx.moveTo(x1, zeroY);
      ctx.lineTo(x1, y1);
      ctx.lineTo(x2, y2);
      ctx.lineTo(x2, zeroY);
      ctx.closePath();
      ctx.fill();
    }
  }

  function renderLapsTrackMap(points) {
    const res = setupCanvas(el.lapsMapCanvas, 460);
    if (!res || !points || points.length < 5) return;
    const { ctx, width: W, height: H } = res;

    ctx.clearRect(0, 0, W, H);

    // Compute bounding box
    let minX = Infinity, maxX = -Infinity, minZ = Infinity, maxZ = -Infinity;
    points.forEach((p) => {
      if (p.x < minX) minX = p.x;
      if (p.x > maxX) maxX = p.x;
      if (p.z < minZ) minZ = p.z;
      if (p.z > maxZ) maxZ = p.z;
    });

    const rangeX = maxX - minX || 100;
    const rangeZ = maxZ - minZ || 100;
    const pad = 35;
    const availW = W - pad * 2;
    const availH = H - pad * 2;
    const scale = Math.min(availW / rangeX, availH / rangeZ);

    const toScreen = (x, z) => {
      const scrX = pad + (x - minX) * scale + (availW - rangeX * scale) / 2;
      const scrZ = H - (pad + (z - minZ) * scale + (availH - rangeZ * scale) / 2);
      return [scrX, scrZ];
    };

    // Draw track path segments colored by delta
    ctx.lineWidth = 4.5;
    ctx.lineCap = 'round';
    ctx.lineJoin = 'round';

    for (let i = 0; i < points.length - 1; i++) {
      const [x1, y1] = toScreen(points[i].x, points[i].z);
      const [x2, y2] = toScreen(points[i + 1].x, points[i + 1].z);
      const d = points[i].delta;

      let strokeColor = '#00e5ff'; // neutral
      if (d < -0.05) {
        strokeColor = '#00e676'; // faster
      } else if (d > 0.05) {
        strokeColor = '#ff3d00'; // slower
      }

      ctx.strokeStyle = strokeColor;
      ctx.beginPath();
      ctx.moveTo(x1, y1);
      ctx.lineTo(x2, y2);
      ctx.stroke();
    }

    // Finish line mark (first point)
    const [startX, startZ] = toScreen(points[0].x, points[0].z);
    ctx.fillStyle = '#ffffff';
    ctx.beginPath();
    ctx.arc(startX, startZ, 6, 0, Math.PI * 2);
    ctx.fill();
    ctx.strokeStyle = '#ff1744';
    ctx.lineWidth = 2;
    ctx.stroke();
  }

  function renderCornerBreakdown(corners) {
    if (!el.cornerBreakdownTbody) return;
    el.cornerBreakdownTbody.innerHTML = '';

    if (!corners || corners.length === 0) {
      el.cornerBreakdownTbody.innerHTML = '<tr class="empty-row"><td colspan="8">No corner delta data available.</td></tr>';
      if (el.cornerCountBadge) el.cornerCountBadge.textContent = '0 CORNERS';
      return;
    }

    if (el.cornerCountBadge) el.cornerCountBadge.textContent = `${corners.length} CORNERS`;

    corners.forEach((c) => {
      const tr = document.createElement('tr');
      const timeGained = c.time_gained_s;
      const isGained = timeGained <= 0;
      const signTime = timeGained > 0 ? '+' : '';
      const signSpd = c.speed_diff_kph > 0 ? '+' : '';

      tr.innerHTML = `
        <td style="font-weight: 700; color: var(--text-main);">${c.corner}</td>
        <td>${c.distance_m}m</td>
        <td>${c.min_speed_lap} kph</td>
        <td style="color: var(--text-muted);">${c.min_speed_ref} kph</td>
        <td class="corner-spd-diff ${c.speed_diff_kph >= 0 ? 'positive' : 'negative'}">${signSpd}${c.speed_diff_kph} kph</td>
        <td>${c.braking_m}m</td>
        <td>${c.throttle_pickup_m}m</td>
        <td class="corner-delta ${isGained ? 'gained' : 'lost'}">${signTime}${timeGained.toFixed(3)}s</td>
      `;
      el.cornerBreakdownTbody.appendChild(tr);
    });
  }

  function fetchStints(sessionId) {
    const token = getToken();
    const url = `/api/sessions/${sessionId}/stints?token=${encodeURIComponent(token)}`;

    fetch(url)
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return res.json();
      })
      .then((stints) => {
        currentStints = stints || [];
        populateStintSelect();
      })
      .catch((err) => {
        console.warn('[Dashboard] Could not fetch stints:', err);
      });
  }

  function populateStintSelect() {
    if (!el.stintSelect) return;
    el.stintSelect.innerHTML = '';

    if (currentStints.length === 0) {
      el.stintSelect.innerHTML = '<option value="">No stints</option>';
      renderSelectedStint(null);
      return;
    }

    currentStints.forEach((st) => {
      const opt = document.createElement('option');
      opt.value = st.stint_id;
      opt.textContent = `Stint ${st.stint_id + 1} (${st.compound_name || 'Tyre'}, ${st.lap_count} laps)`;
      el.stintSelect.appendChild(opt);
    });

    el.stintSelect.value = currentStints[0].stint_id;
    renderSelectedStint(currentStints[0].stint_id);
  }

  function renderSelectedStint(stintId) {
    if (stintId === null || currentStints.length === 0) {
      if (el.stintTimeSlope) el.stintTimeSlope.textContent = '-- s/lap';
      if (el.stintWearSlope) el.stintWearSlope.textContent = '-- %/lap';
      if (el.stintLapCount) el.stintLapCount.textContent = '0';
      if (el.stintCompoundName) el.stintCompoundName.textContent = 'Compound: --';
      if (el.stintLapsTbody) {
        el.stintLapsTbody.innerHTML = '<tr class="empty-row"><td colspan="7">No stint data recorded.</td></tr>';
      }
      return;
    }

    const st = currentStints.find((s) => s.stint_id === stintId) || currentStints[0];

    if (el.stintTimeSlope) el.stintTimeSlope.textContent = `${st.time_deg_s_per_lap > 0 ? '+' : ''}${st.time_deg_s_per_lap.toFixed(3)} s/lap`;
    if (el.stintWearSlope) el.stintWearSlope.textContent = `+${st.wear_deg_pct_per_lap.toFixed(2)} %/lap`;
    if (el.stintLapCount) el.stintLapCount.textContent = `${st.valid_lap_count} / ${st.lap_count}`;
    if (el.stintCompoundName) el.stintCompoundName.textContent = `Compound: ${st.compound_name || 'Unknown'}`;

    if (el.stintLapsTbody) {
      el.stintLapsTbody.innerHTML = '';
      (st.laps || []).forEach((l) => {
        const tr = document.createElement('tr');
        tr.innerHTML = `
          <td style="font-weight: 700;">Lap ${l.lap_num}</td>
          <td class="mono" style="font-weight: 800; color: var(--color-cyan);">${formatLapTime(l.lap_time_s)}</td>
          <td class="mono">${l.sector1_s ? l.sector1_s.toFixed(3) : '-'}</td>
          <td class="mono">${l.sector2_s ? l.sector2_s.toFixed(3) : '-'}</td>
          <td class="mono">${l.sector3_s ? l.sector3_s.toFixed(3) : '-'}</td>
          <td class="mono">${l.tyre_wear_end_pct !== null ? `${Math.round(l.tyre_wear_end_pct)}%` : '-'}</td>
          <td>${l.is_valid ? '<span style="color: var(--color-green);">✓</span>' : '<span style="color: var(--color-red);">✗</span>'}</td>
        `;
        el.stintLapsTbody.appendChild(tr);
      });
    }
  }

  function renderOvertakes(overtakes) {
    const tbody = document.getElementById('overtakes-tbody');
    if (!tbody) return;
    if (!overtakes || overtakes.length === 0) {
      tbody.innerHTML = '<tr class="empty-row"><td colspan="11">No overtakes recorded for this session.</td></tr>';
      return;
    }
    tbody.innerHTML = '';
    overtakes.forEach((ot) => {
      tbody.appendChild(_buildOvertakeRow(ot));
    });
  }

  function appendOvertakeRow(ot) {
    const tbody = document.getElementById('overtakes-tbody');
    if (!tbody) return;
    const emptyRow = tbody.querySelector('.empty-row');
    if (emptyRow) emptyRow.remove();
    tbody.insertBefore(_buildOvertakeRow(ot), tbody.firstChild);
  }

  function _buildOvertakeRow(ot) {
    const tr = document.createElement('tr');
    const isPass = (ot.direction === 'pass' || ot.move === 'overtake');
    const moveColor = isPass ? 'var(--color-green)' : 'var(--color-red)';
    const moveLabel = isPass ? 'PASS' : 'LOST';

    let classBadge = '<span class="badge badge-clean">clean</span>';
    if (ot.classification === 'brave') {
      classBadge = '<span class="badge" style="color: #ffaa00; font-weight: 700;">brave</span>';
    } else if (ot.classification === 'divebomb') {
      classBadge = '<span class="badge" style="color: #ff4444; font-weight: 700;">divebomb</span>';
    } else if (ot.classification === 'divebomb_contact') {
      classBadge = '<span class="badge" style="color: #ff0055; font-weight: 800;">divebomb (contact)</span>';
    } else if (ot.classification === 'overtake_switchback') {
      classBadge = '<span class="badge" style="color: #aa44ff; font-weight: 700;">switchback</span>';
    } else if (ot.classification === 'passed_stricken_car') {
      classBadge = '<span class="badge" style="color: #888888; font-weight: 600;">stricken</span>';
    }

    const bDelta = (ot.braking_delta_m != null) ? `${ot.braking_delta_m > 0 ? '+' : ''}${ot.braking_delta_m.toFixed(1)}m` : '-';
    const sGap = (ot.start_gap_m != null) ? `${ot.start_gap_m.toFixed(1)}m` : '-';
    const driverName = ot.other_driver_name || `Car ${ot.other_vehicle_idx ?? ot.car_idx ?? '?'}`;
    const cornerName = ot.corner_name || '-';

    tr.innerHTML = `
      <td style="font-weight: 700;">Lap ${ot.lap_num || '-'}</td>
      <td class="bold">${driverName}</td>
      <td class="bold" style="color: ${moveColor};">${moveLabel}</td>
      <td>${classBadge}</td>
      <td>${cornerName}</td>
      <td class="mono">${bDelta}</td>
      <td class="mono">${sGap}</td>
      <td>${ot.overlap_at_turn_in ? '<span style="color: var(--color-green);">✓</span>' : '<span style="color: #666;">-</span>'}</td>
      <td>${ot.ran_deep ? '<span style="color: #ffaa00;">YES</span>' : '<span style="color: #666;">-</span>'}</td>
      <td>${ot.contact ? '<span style="color: var(--color-red); font-weight: 700;">COLL</span>' : '<span style="color: #666;">-</span>'}</td>
      <td>${ot.forced_off ? '<span style="color: #ff4444;">YES</span>' : '<span style="color: #666;">-</span>'}</td>
    `;
    return tr;
  }

  // ==========================================================================
  // Setups Library Module
  // ==========================================================================
  let loadedRankedSetups = [];
  let currentActiveSetup = null;
  let activeDetailsSetup = null;
  let currentImportMode = 'freetext';
  let lastParsedImport = null;

  function initSetupsTab() {
    if (el.btnSetupsLiveSync) {
      el.btnSetupsLiveSync.addEventListener('click', () => fetchSetups(true));
    }

    [el.setupsFilterTrack, el.setupsFilterClass, el.setupsFilterConditions, el.setupsFilterSession].forEach(elem => {
      if (elem) elem.addEventListener('change', () => fetchSetups(false));
    });

    if (el.btnRunSetupDiff) {
      el.btnRunSetupDiff.addEventListener('click', () => {
        const idA = el.diffSelectA.value;
        const idB = el.diffSelectB.value;
        if (idA && idB) runSetupDiff(idA, idB);
      });
    }

    if (el.btnExportMountedSheet) {
      el.btnExportMountedSheet.addEventListener('click', () => {
        exportSetupFile('current', 'sheet');
      });
    }

    if (el.btnExportMountedJson) {
      el.btnExportMountedJson.addEventListener('click', () => {
        exportSetupFile('current', 'json');
      });
    }

    // Details modal
    if (el.btnCloseDetailsModal) el.btnCloseDetailsModal.addEventListener('click', closeDetailsModal);
    if (el.btnCloseDetailsBtn) el.btnCloseDetailsBtn.addEventListener('click', closeDetailsModal);
    if (el.btnModalExportSheet) {
      el.btnModalExportSheet.addEventListener('click', () => {
        if (activeDetailsSetup) exportSetupFile(activeDetailsSetup.id, 'sheet');
      });
    }
    if (el.btnModalExportJson) {
      el.btnModalExportJson.addEventListener('click', () => {
        if (activeDetailsSetup) exportSetupFile(activeDetailsSetup.id, 'json');
      });
    }

    // Setup Entry Form triggers
    if (el.btnOpenNewSetup) {
      el.btnOpenNewSetup.addEventListener('click', () => openSetupFormModal('new'));
    }
    if (el.btnEditMountedSetup) {
      el.btnEditMountedSetup.addEventListener('click', () => {
        if (currentActiveSetup) {
          openSetupFormModal('edit', currentActiveSetup);
        } else {
          openSetupFormModal('new');
        }
      });
    }

    if (el.btnCloseSetupFormModal) el.btnCloseSetupFormModal.addEventListener('click', closeSetupFormModal);
    if (el.btnCancelSetupForm) el.btnCancelSetupForm.addEventListener('click', closeSetupFormModal);
    if (el.btnClearSetupForm) el.btnClearSetupForm.addEventListener('click', clearSetupFormFields);
    if (el.btnSaveSetupForm) el.btnSaveSetupForm.addEventListener('click', () => saveSetupForm(false));

    if (el.btnStartBlank) el.btnStartBlank.addEventListener('click', startFromBlank);
    if (el.btnStartCopyCurrent) el.btnStartCopyCurrent.addEventListener('click', startFromCurrentCar);
    if (el.selectDuplicateSetup) {
      el.selectDuplicateSetup.addEventListener('change', (e) => {
        if (e.target.value) startFromDuplicate(e.target.value);
      });
    }
    if (el.btnSyncLiveMeta) el.btnSyncLiveMeta.addEventListener('click', syncLiveMetaToForm);

    if (el.setupInputClass) {
      el.setupInputClass.addEventListener('change', (e) => {
        const cls = parseInt(e.target.value, 10);
        ensureSetupSchema(cls);
      });
    }

    // Duplicate modal triggers
    if (el.btnDupOpenExisting) el.btnDupOpenExisting.addEventListener('click', openDuplicateExisting);
    if (el.btnDupSaveAnyway) el.btnDupSaveAnyway.addEventListener('click', () => saveSetupForm(true));
    if (el.btnDupCancel) el.btnDupCancel.addEventListener('click', closeDuplicateModal);
    if (el.btnCloseDupModal) el.btnCloseDupModal.addEventListener('click', closeDuplicateModal);

    // Import modal
    initImportModal();
  }

  function exportSetupFile(setupId, format) {
    const token = getToken();
    const url = `/api/setups/${encodeURIComponent(setupId)}/export?format=${format}&token=${encodeURIComponent(token)}`;
    window.open(url, '_blank');
  }

  function fetchCurrentSetup() {
    const token = getToken();
    fetch(`/api/setups/current?token=${encodeURIComponent(token)}`)
      .then(res => res.json())
      .then(data => {
        if (!data || !data.active || !data.params) {
          if (el.mountedSetupName) el.mountedSetupName.textContent = 'No Telemetry Setup Captured';
          if (el.mountedSetupFp) el.mountedSetupFp.textContent = '--';
          return;
        }
        currentActiveSetup = data;
        const p = data.params;
        if (el.mountedSetupName) el.mountedSetupName.textContent = `Car Setup (${data.id})`;
        if (el.mountedSetupFp) el.mountedSetupFp.textContent = `FP: ${data.fingerprint || '--'}`;

        if (el.statMountedWings) el.statMountedWings.textContent = `${p.front_wing ?? '--'} / ${p.rear_wing ?? '--'}`;
        if (el.statMountedDiff) el.statMountedDiff.textContent = `${p.on_throttle ?? '--'}% / ${p.off_throttle ?? '--'}%`;
        if (el.statMountedBias) el.statMountedBias.textContent = `${p.brake_bias ?? '--'}%`;
        if (el.statMountedTyresF) el.statMountedTyresF.textContent = `${p.front_left_tyre_pressure ?? '--'} / ${p.front_right_tyre_pressure ?? '--'} psi`;
        if (el.statMountedTyresR) el.statMountedTyresR.textContent = `${p.rear_left_tyre_pressure ?? '--'} / ${p.rear_right_tyre_pressure ?? '--'} psi`;
      })
      .catch(err => console.debug('Current setup fetch error:', err));
  }

  function fetchSetups(useLiveSession = false) {
    const token = getToken();
    let url = `/api/setups?token=${encodeURIComponent(token)}`;

    if (!useLiveSession) {
      const trackVal = el.setupsFilterTrack ? el.setupsFilterTrack.value : '';
      const classVal = el.setupsFilterClass ? el.setupsFilterClass.value : '';
      const condVal = el.setupsFilterConditions ? el.setupsFilterConditions.value : '';
      const sessVal = el.setupsFilterSession ? el.setupsFilterSession.value : '';

      if (trackVal) url += `&track_id=${encodeURIComponent(trackVal)}`;
      if (classVal !== '') url += `&car_class=${encodeURIComponent(classVal)}`;
      if (condVal) url += `&conditions=${encodeURIComponent(condVal)}`;
      if (sessVal) url += `&session_type=${encodeURIComponent(sessVal)}`;
    }

    fetch(url)
      .then(res => res.json())
      .then(data => {
        if (!data) return;
        loadedRankedSetups = data.setups || [];

        // Populate track dropdown if empty
        if (el.setupsFilterTrack && data.tracks && el.setupsFilterTrack.options.length <= 1) {
          el.setupsFilterTrack.innerHTML = '';
          data.tracks.forEach(t => {
            const opt = document.createElement('option');
            opt.value = t.id;
            opt.textContent = t.name;
            el.setupsFilterTrack.appendChild(opt);
          });
        }

        // Also populate import and setup form track dropdowns if empty
        if (data.tracks) {
          if (el.importTrack && el.importTrack.options.length === 0) {
            data.tracks.forEach(t => {
              const opt = document.createElement('option');
              opt.value = t.id;
              opt.textContent = t.name;
              el.importTrack.appendChild(opt);
            });
          }
          if (el.setupInputTrack && el.setupInputTrack.options.length === 0) {
            data.tracks.forEach(t => {
              const opt = document.createElement('option');
              opt.value = t.id;
              opt.textContent = t.name;
              el.setupInputTrack.appendChild(opt);
            });
          }
        }

        // Sync filters if useLiveSession was triggered
        if (useLiveSession && data.current_session) {
          const cs = data.current_session;
          if (el.setupsFilterTrack) el.setupsFilterTrack.value = cs.track_id;
          if (el.setupsFilterClass) el.setupsFilterClass.value = cs.car_class;
          if (el.setupsFilterConditions) el.setupsFilterConditions.value = cs.conditions;
          if (el.setupsFilterSession) el.setupsFilterSession.value = cs.session_type;
        }

        renderRankedSetups(loadedRankedSetups, data.current_setup_id);
        updateDiffDropdowns(loadedRankedSetups);
      })
      .catch(err => console.error('Error fetching setups:', err));
  }

  function renderRankedSetups(setupsList, currentSetupId) {
    if (!el.setupsRankTbody) return;
    el.setupsRankTbody.innerHTML = '';

    if (!setupsList || setupsList.length === 0) {
      el.setupsRankTbody.innerHTML = `
        <tr class="empty-row">
          <td colspan="8">No setups found matching current filters. Complete laps in session or import a setup.</td>
        </tr>
      `;
      return;
    }

    setupsList.forEach((s) => {
      const tr = document.createElement('tr');
      const isCurrent = s.is_current || (currentSetupId && String(currentSetupId) === String(s.id));
      const isUntested = s.untested || s.valid_laps_count === 0;

      let statusBadge = '';
      if (isCurrent) {
        statusBadge = '<span class="badge-status current">ACTIVE ON CAR</span>';
      } else if (isUntested) {
        statusBadge = '<span class="badge-status untested">UNTESTED</span>';
      } else {
        statusBadge = '<span class="badge-status tested">TESTED</span>';
      }

      const sourceBadge = `<span class="badge-source ${s.source === 'captured' ? 'captured' : 'imported'}">${s.source}</span>`;
      const bestLapStr = s.best_lap_s ? formatLapTime(s.best_lap_s) : '--:--.---';
      const medianLapStr = s.median_lap_s ? formatLapTime(s.median_lap_s) : (s.valid_laps_count > 0 ? `${s.valid_laps_count}/3 laps` : '-');

      tr.innerHTML = `
        <td style="font-weight: 800; color: ${s.rank ? 'var(--color-cyan)' : 'var(--text-muted)'};">${s.rank ? `P${s.rank}` : '-'}</td>
        <td>
          <div style="font-weight: 700; color: var(--text-main);">${s.name || `Setup ${s.id}`} ${sourceBadge}</div>
          <div style="font-size: 11px; color: var(--text-muted);">${s.source_note || s.fingerprint}</div>
        </td>
        <td style="text-transform: capitalize;">${s.source}</td>
        <td class="mono" style="font-weight: 800; color: ${s.best_lap_s ? 'var(--color-green)' : 'inherit'};">${bestLapStr}</td>
        <td class="mono">${medianLapStr}</td>
        <td class="mono">${s.valid_laps_count} valid / ${s.total_laps_count} tot</td>
        <td>${statusBadge}</td>
        <td style="text-align: right; white-space: nowrap;">
          <button class="btn-sm btn-action-diff" data-id="${s.id}" title="Compare this setup">DIFF</button>
          <button class="btn-sm btn-action-edit" data-id="${s.id}" title="${s.source === 'captured' ? 'Edit copy of captured setup' : 'Edit setup'}">${s.source === 'captured' ? 'COPY/EDIT' : 'EDIT'}</button>
          ${s.source !== 'captured' ? `<button class="btn-sm btn-action-del" data-id="${s.id}" title="Delete setup" style="color: var(--color-red);">DEL</button>` : ''}
          <button class="btn-sm btn-action-view" data-id="${s.id}" title="View all parameters">VIEW</button>
          <button class="btn-sm btn-action-sheet" data-id="${s.id}" title="Export Plain-Text Sheet">TXT</button>
          <button class="btn-sm btn-action-json" data-id="${s.id}" title="Export JSON">JSON</button>
        </td>
      `;

      // Event listeners for action buttons
      tr.querySelector('.btn-action-diff').addEventListener('click', () => {
        if (el.diffSelectA) el.diffSelectA.value = 'current';
        if (el.diffSelectB) el.diffSelectB.value = s.id;
        runSetupDiff('current', s.id);
        const diffPanel = document.querySelector('.setups-diff-panel');
        if (diffPanel) diffPanel.scrollIntoView({ behavior: 'smooth' });
      });

      const editBtn = tr.querySelector('.btn-action-edit');
      if (editBtn) {
        editBtn.addEventListener('click', () => openSetupFormModal('edit', s));
      }

      const delBtn = tr.querySelector('.btn-action-del');
      if (delBtn) {
        delBtn.addEventListener('click', () => deleteSetupItem(s.id, s.name));
      }

      tr.querySelector('.btn-action-view').addEventListener('click', () => openSetupDetails(s.id));
      tr.querySelector('.btn-action-sheet').addEventListener('click', () => exportSetupFile(s.id, 'sheet'));
      tr.querySelector('.btn-action-json').addEventListener('click', () => exportSetupFile(s.id, 'json'));

      el.setupsRankTbody.appendChild(tr);
    });
  }

  function updateDiffDropdowns(setupsList) {
    if (!el.diffSelectA || !el.diffSelectB) return;

    const valA = el.diffSelectA.value;
    const valB = el.diffSelectB.value;

    el.diffSelectA.innerHTML = '<option value="current">Current Mounted Setup</option>';
    el.diffSelectB.innerHTML = '<option value="">Select setup to compare...</option><option value="current">Current Mounted Setup</option>';

    if (el.selectDuplicateSetup) {
      el.selectDuplicateSetup.innerHTML = '<option value="">Choose setup...</option>';
    }

    setupsList.forEach(s => {
      const optA = document.createElement('option');
      optA.value = s.id;
      optA.textContent = `${s.rank ? `P${s.rank} ` : ''}${s.name} (${s.source})`;
      el.diffSelectA.appendChild(optA);

      const optB = document.createElement('option');
      optB.value = s.id;
      optB.textContent = `${s.rank ? `P${s.rank} ` : ''}${s.name} (${s.source})`;
      el.diffSelectB.appendChild(optB);

      if (el.selectDuplicateSetup) {
        const optDup = document.createElement('option');
        optDup.value = s.id;
        optDup.textContent = `${s.name || `Setup ${s.id}`} (${s.source})`;
        el.selectDuplicateSetup.appendChild(optDup);
      }
    });

    if (valA) el.diffSelectA.value = valA;
    if (valB) el.diffSelectB.value = valB;
  }

  function runSetupDiff(idA, idB) {
    const token = getToken();
    fetch(`/api/setups/diff?id1=${encodeURIComponent(idA)}&id2=${encodeURIComponent(idB)}&token=${encodeURIComponent(token)}`)
      .then(res => res.json())
      .then(diffData => {
        if (!diffData || !diffData.diffs) return;
        if (el.diffResultsWrap) el.diffResultsWrap.classList.remove('hidden');

        if (el.thDiffA) el.thDiffA.textContent = diffData.setup_a_name;
        if (el.thDiffB) el.thDiffB.textContent = diffData.setup_b_name;
        if (el.diffChangesCount) {
          el.diffChangesCount.textContent = `${diffData.changed_count} PARAMETERS CHANGED`;
        }

        if (el.setupsDiffTbody) {
          el.setupsDiffTbody.innerHTML = '';
          const keys = Object.keys(diffData.diffs);

          keys.forEach(k => {
            const item = diffData.diffs[k];
            const tr = document.createElement('tr');
            if (item.changed) tr.classList.add('diff-row-changed');

            let deltaHtml = '<span class="delta-badge same">No change</span>';
            if (item.changed) {
              if (item.delta !== null && item.delta !== undefined) {
                const isPos = item.delta > 0;
                deltaHtml = `<span class="delta-badge ${isPos ? 'pos' : 'neg'}">${isPos ? '+' : ''}${item.delta}</span>`;
              } else {
                deltaHtml = '<span class="delta-badge pos">Modified</span>';
              }
            }

            const formatVal = (v) => (v === null || v === undefined) ? '--' : v;

            tr.innerHTML = `
              <td style="font-family: var(--font-mono); font-size: 12px;">${k}</td>
              <td class="mono">${formatVal(item.val_a)}</td>
              <td class="mono">${formatVal(item.val_b)}</td>
              <td>${deltaHtml}</td>
            `;
            el.setupsDiffTbody.appendChild(tr);
          });
        }
      })
      .catch(err => console.error('Setup diff error:', err));
  }

  function openSetupDetails(setupId) {
    const token = getToken();
    fetch(`/api/setups/${encodeURIComponent(setupId)}?token=${encodeURIComponent(token)}`)
      .then(res => res.json())
      .then(data => {
        if (!data) return;
        activeDetailsSetup = data;
        if (el.modalDetailsTitle) {
          el.modalDetailsTitle.textContent = `${data.name || 'Setup'} (ID: ${data.id})`;
        }
        if (el.modalDetailsBody) {
          const params = data.params || {};
          let html = `
            <div style="font-size: 12px; color: var(--text-muted); margin-bottom: 12px;">
              Source: <b style="color: var(--text-main);">${data.source}</b> |
              Conditions: <b style="color: var(--text-main);">${(data.conditions || 'dry').toUpperCase()}</b> |
              Fingerprint: <code style="color: var(--color-cyan);">${data.fingerprint}</code>
            </div>
            <div class="preview-params-grid" style="max-height: 400px;">
          `;
          Object.keys(params).sort().forEach(k => {
            html += `
              <div class="param-pill">
                <span class="pill-key">${k}</span>
                <span class="pill-val">${params[k]}</span>
              </div>
            `;
          });
          html += '</div>';
          el.modalDetailsBody.innerHTML = html;
        }
        if (el.setupDetailsModal) el.setupDetailsModal.classList.remove('hidden');
      })
      .catch(err => console.error('Error fetching setup details:', err));
  }

  function closeDetailsModal() {
    if (el.setupDetailsModal) el.setupDetailsModal.classList.add('hidden');
    activeDetailsSetup = null;
  }

  function initImportModal() {
    if (el.btnOpenImport) {
      el.btnOpenImport.addEventListener('click', () => {
        if (el.setupImportModal) el.setupImportModal.classList.remove('hidden');
        if (el.importPreviewBox) el.importPreviewBox.classList.add('hidden');
        if (el.btnConfirmSaveImport) el.btnConfirmSaveImport.disabled = true;
      });
    }

    if (el.btnCloseImportModal) {
      el.btnCloseImportModal.addEventListener('click', () => {
        if (el.setupImportModal) el.setupImportModal.classList.add('hidden');
      });
    }

    // Tab buttons in import modal
    if (el.tabImportFreetext) {
      el.tabImportFreetext.addEventListener('click', () => {
        currentImportMode = 'freetext';
        el.tabImportFreetext.classList.add('active');
        el.tabImportJson.classList.remove('active');
        el.tabImportCsv.classList.remove('active');
        el.importContentLabel.textContent = 'PASTE FREE TEXT (E.G. YOUTUBE / REDDIT SETUP)';
        el.importContent.placeholder = 'Paste free text (e.g. wings 38/32, diff 55/50, camber -2.5/-1.0, brakes 100/54)...';
      });
    }

    if (el.tabImportJson) {
      el.tabImportJson.addEventListener('click', () => {
        currentImportMode = 'json';
        el.tabImportJson.classList.add('active');
        el.tabImportFreetext.classList.remove('active');
        el.tabImportCsv.classList.remove('active');
        el.importContentLabel.textContent = 'PASTE JSON SETUP';
        el.importContent.placeholder = '{\n  "params": {\n    "front_wing": 38,\n    "rear_wing": 32\n  }\n}';
      });
    }

    if (el.tabImportCsv) {
      el.tabImportCsv.addEventListener('click', () => {
        currentImportMode = 'csv';
        el.tabImportCsv.classList.add('active');
        el.tabImportFreetext.classList.remove('active');
        el.tabImportJson.classList.remove('active');
        el.importContentLabel.textContent = 'PASTE CSV SETUP DATA (parameter, value)';
        el.importContent.placeholder = 'front_wing, 38\nrear_wing, 32\non_throttle, 55\nbrake_bias, 54';
      });
    }

    // Parse & Validate button
    if (el.btnParseValidate) {
      el.btnParseValidate.addEventListener('click', () => {
        const content = el.importContent ? el.importContent.value.trim() : '';
        if (!content) {
          alert('Please paste setup content to parse.');
          return;
        }

        if (el.importStatusSpinner) el.importStatusSpinner.classList.remove('hidden');
        if (el.btnParseValidate) el.btnParseValidate.disabled = true;

        const payload = {
          mode: currentImportMode,
          content: content,
          save: false, // validate only
          name: el.importName ? el.importName.value.trim() : '',
          track_id: el.importTrack ? parseInt(el.importTrack.value, 10) : 7,
          car_class: el.importClass ? parseInt(el.importClass.value, 10) : 0,
          conditions: el.importConditions ? el.importConditions.value : 'dry',
          session_type: el.importSession ? el.importSession.value : 'race',
          source_note: el.importNote ? el.importNote.value.trim() : '',
        };

        const token = getToken();
        fetch(`/api/setups/import?token=${encodeURIComponent(token)}`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        })
          .then(res => res.json())
          .then(resData => {
            if (el.importStatusSpinner) el.importStatusSpinner.classList.add('hidden');
            if (el.btnParseValidate) el.btnParseValidate.disabled = false;
            lastParsedImport = resData;
            renderImportPreview(resData);
          })
          .catch(err => {
            if (el.importStatusSpinner) el.importStatusSpinner.classList.add('hidden');
            if (el.btnParseValidate) el.btnParseValidate.disabled = false;
            alert('Parsing error: ' + err);
          });
      });
    }

    // Confirm & Save button
    if (el.btnConfirmSaveImport) {
      el.btnConfirmSaveImport.addEventListener('click', () => {
        if (!lastParsedImport) return;

        const content = el.importContent ? el.importContent.value.trim() : '';
        const payload = {
          mode: currentImportMode,
          content: content,
          save: true, // confirmed save
          name: el.importName ? el.importName.value.trim() : '',
          track_id: el.importTrack ? parseInt(el.importTrack.value, 10) : 7,
          car_class: el.importClass ? parseInt(el.importClass.value, 10) : 0,
          conditions: el.importConditions ? el.importConditions.value : 'dry',
          session_type: el.importSession ? el.importSession.value : 'race',
          source_note: el.importNote ? el.importNote.value.trim() : '',
        };

        const token = getToken();
        fetch(`/api/setups/import?token=${encodeURIComponent(token)}`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        })
          .then(res => res.json())
          .then(saveRes => {
            if (saveRes && saveRes.saved) {
              if (el.setupImportModal) el.setupImportModal.classList.add('hidden');
              alert(`Setup saved successfully! (ID: ${saveRes.setup.id})`);
              fetchSetups(false);
            } else {
              alert('Save failed: ' + JSON.stringify(saveRes));
            }
          })
          .catch(err => alert('Save error: ' + err));
      });
    }
  }

  function renderImportPreview(data) {
    if (!el.importPreviewBox) return;
    el.importPreviewBox.classList.remove('hidden');

    const params = data.params || {};
    const missing = data.missing || [];
    const outOfRange = data.out_of_range || {};

    if (el.previewAlerts) {
      el.previewAlerts.innerHTML = '';
      if (Object.keys(outOfRange).length > 0) {
        const div = document.createElement('div');
        div.className = 'alert-item danger';
        div.textContent = `Warning: ${Object.keys(outOfRange).length} parameters are outside valid observed ranges!`;
        el.previewAlerts.appendChild(div);
      }
      if (missing.length > 0) {
        const div = document.createElement('div');
        div.className = 'alert-item warning';
        div.textContent = `Notice: ${missing.length} parameters not specified in import. Left empty without guessing.`;
        el.previewAlerts.appendChild(div);
      }
    }

    if (el.previewParamsGrid) {
      el.previewParamsGrid.innerHTML = '';
      Object.keys(params).sort().forEach(k => {
        const isOutOfRange = (k in outOfRange);
        const pill = document.createElement('div');
        pill.className = `param-pill ${isOutOfRange ? 'out-of-range' : ''}`;
        pill.innerHTML = `
          <span class="pill-key">${k}</span>
          <span class="pill-val">${params[k]}${isOutOfRange ? ' ⚠️' : ''}</span>
        `;
        el.previewParamsGrid.appendChild(pill);
      });

      missing.forEach(k => {
        const pill = document.createElement('div');
        pill.className = 'param-pill missing';
        pill.innerHTML = `
          <span class="pill-key">${k}</span>
          <span class="pill-val">[empty]</span>
        `;
        el.previewParamsGrid.appendChild(pill);
      });
    }

    if (el.btnConfirmSaveImport) {
      el.btnConfirmSaveImport.disabled = false;
    }
  }

  // ==========================================================================
  // Setup Entry Form & Modal
  // ==========================================================================
  const SETUP_DRAFT_KEY = 'race_engineer_setup_draft';
  let setupFormMode = 'new'; // 'new' | 'edit'
  let setupFormEditingSetup = null;
  let currentSetupSchema = null;
  let lastDuplicateData = null;
  let setupAutosaveTimer = null;

  function openSetupFormModal(mode, setup = null) {
    setupFormMode = mode;
    setupFormEditingSetup = setup;

    if (el.setupFormModalTitle) {
      if (mode === 'edit') {
        if (setup && setup.source === 'captured') {
          el.setupFormModalTitle.textContent = `EDIT CAPTURED SETUP (COPY)`;
          if (el.setupFormModeBadge) {
            el.setupFormModeBadge.textContent = 'IMPORT COPY';
            el.setupFormModeBadge.className = 'badge warning';
          }
          if (el.setupReadOnlyNotice) el.setupReadOnlyNotice.classList.remove('hidden');
        } else {
          el.setupFormModalTitle.textContent = `EDIT SETUP #${setup ? setup.id : ''}`;
          if (el.setupFormModeBadge) {
            el.setupFormModeBadge.textContent = 'EDIT';
            el.setupFormModeBadge.className = 'badge primary';
          }
          if (el.setupReadOnlyNotice) el.setupReadOnlyNotice.classList.add('hidden');
        }
      } else {
        el.setupFormModalTitle.textContent = 'NEW CAR SETUP';
        if (el.setupFormModeBadge) {
          el.setupFormModeBadge.textContent = 'NEW';
          el.setupFormModeBadge.className = 'badge success';
        }
        if (el.setupReadOnlyNotice) el.setupReadOnlyNotice.classList.add('hidden');
      }
    }

    // Populate car class options if empty
    if (el.setupInputClass && el.setupInputClass.options.length === 0) {
      el.setupInputClass.innerHTML = `
        <option value="0">F1 Modern</option>
        <option value="2">F2</option>
        <option value="1">F1 Classic</option>
      `;
    }

    // Populate track options from filter if empty
    if (el.setupInputTrack && el.setupInputTrack.options.length === 0 && el.setupsFilterTrack) {
      Array.from(el.setupsFilterTrack.options).forEach(opt => {
        const newOpt = document.createElement('option');
        newOpt.value = opt.value;
        newOpt.textContent = opt.textContent;
        el.setupInputTrack.appendChild(newOpt);
      });
    }

    const carClass = setup && setup.car_class !== undefined ? setup.car_class : (el.setupInputClass ? parseInt(el.setupInputClass.value, 10) : 0);
    ensureSetupSchema(carClass).then(() => {
      if (mode === 'edit' && setup) {
        populateFormFromSetup(setup, false);
      } else {
        const restored = restoreSetupDraft();
        if (!restored) {
          clearSetupFormFields();
          syncLiveMetaToForm();
        }
      }
      updateLiveDiffs();
      if (el.setupFormModal) el.setupFormModal.classList.remove('hidden');
    });
  }

  function closeSetupFormModal() {
    if (el.setupFormModal) el.setupFormModal.classList.add('hidden');
    setupFormEditingSetup = null;
  }

  function clearSetupFormFields() {
    document.querySelectorAll('.param-input').forEach(input => {
      input.value = '';
      input.classList.remove('out-of-bounds');
    });
    if (el.setupInputName) el.setupInputName.value = '';
    if (el.setupInputSourceNote) el.setupInputSourceNote.value = '';
    if (el.setupInputSourceLink) el.setupInputSourceLink.value = '';
    if (el.setupInputNotes) el.setupInputNotes.value = '';
    if (el.btnStartBlank) el.btnStartBlank.classList.add('active');
    if (el.btnStartCopyCurrent) el.btnStartCopyCurrent.classList.remove('active');
    if (el.selectDuplicateSetup) el.selectDuplicateSetup.value = '';
    updateLiveDiffs();
    clearSetupDraft();
  }

  function ensureSetupSchema(carClass = 0) {
    const token = getToken();
    return fetch(`/api/setups/schema?car_class=${carClass}&token=${encodeURIComponent(token)}`)
      .then(res => res.json())
      .then(schema => {
        currentSetupSchema = schema;
        renderSetupFormGroups(schema);
      })
      .catch(err => console.error('Error fetching setup schema:', err));
  }

  function renderSetupFormGroups(schema) {
    if (!el.setupFormGroupsWrap) return;
    el.setupFormGroupsWrap.innerHTML = '';

    const groups = schema.groups || [];
    const parameters = schema.parameters || [];

    // Group parameters by group name
    const byGroup = {};
    groups.forEach(g => { byGroup[g] = []; });
    parameters.forEach(p => {
      if (!byGroup[p.group]) byGroup[p.group] = [];
      byGroup[p.group].push(p);
    });

    let paramIndex = 0;
    const totalParams = parameters.length;

    groups.forEach(groupName => {
      const paramsInGroup = byGroup[groupName] || [];
      if (paramsInGroup.length === 0) return;

      const card = document.createElement('div');
      card.className = 'setup-group-card';

      const h5 = document.createElement('h5');
      h5.className = 'group-card-title';
      h5.textContent = groupName.toUpperCase();
      card.appendChild(h5);

      const rowsWrap = document.createElement('div');
      rowsWrap.className = 'group-params-wrap';

      paramsInGroup.forEach(p => {
        paramIndex++;
        const isLastField = (paramIndex === totalParams);
        const row = document.createElement('div');
        row.className = 'setup-param-row';
        row.dataset.key = p.key;

        const labelWrap = document.createElement('div');
        labelWrap.className = 'param-label-wrap';

        const label = document.createElement('label');
        label.className = 'param-title';
        label.setAttribute('for', `param-input-${p.key}`);
        label.textContent = p.label;
        labelWrap.appendChild(label);

        if (p.unit) {
          const unitSpan = document.createElement('span');
          unitSpan.className = 'param-unit';
          unitSpan.textContent = `[${p.unit}]`;
          labelWrap.appendChild(unitSpan);
        }

        if (p.has_confirmed_range && p.min !== null && p.max !== null) {
          const rangeSpan = document.createElement('span');
          rangeSpan.className = 'range-hint';
          rangeSpan.textContent = `[${p.min} – ${p.max}]`;
          labelWrap.appendChild(rangeSpan);
        } else {
          const unconfirmedSpan = document.createElement('span');
          unconfirmedSpan.className = 'range-unconfirmed-badge';
          unconfirmedSpan.textContent = 'range not confirmed';
          unconfirmedSpan.title = 'Observed range not confirmed for 2026 update; any value allowed';
          labelWrap.appendChild(unconfirmedSpan);
        }

        row.appendChild(labelWrap);

        const controlsWrap = document.createElement('div');
        controlsWrap.className = 'param-controls-wrap';

        const minusBtn = document.createElement('button');
        minusBtn.type = 'button';
        minusBtn.className = 'stepper-btn btn-minus';
        minusBtn.textContent = '−';
        minusBtn.title = `Decrease by ${p.step}`;
        minusBtn.tabIndex = -1;

        const input = document.createElement('input');
        input.type = 'number';
        input.className = 'f1-input param-input';
        input.inputMode = 'decimal';
        input.id = `param-input-${p.key}`;
        input.dataset.key = p.key;
        input.dataset.type = p.type;
        input.dataset.step = p.step;
        input.dataset.hasConfirmedRange = p.has_confirmed_range ? '1' : '0';
        if (p.min !== null && p.min !== undefined) input.dataset.min = p.min;
        if (p.max !== null && p.max !== undefined) input.dataset.max = p.max;
        input.step = p.step;
        input.placeholder = '--';

        const plusBtn = document.createElement('button');
        plusBtn.type = 'button';
        plusBtn.className = 'stepper-btn btn-plus';
        plusBtn.textContent = '+';
        plusBtn.title = `Increase by ${p.step}`;
        plusBtn.tabIndex = -1;

        const diffSpan = document.createElement('span');
        diffSpan.id = `diff-badge-${p.key}`;
        diffSpan.className = 'diff-badge-indicator diff-na';
        diffSpan.textContent = '--';

        minusBtn.addEventListener('click', () => stepParam(p.key, -1));
        plusBtn.addEventListener('click', () => stepParam(p.key, 1));

        input.addEventListener('input', () => onParamInputChanged(input));
        input.addEventListener('change', () => onParamInputChanged(input));

        // Desktop keyboard navigation
        input.addEventListener('keydown', (e) => {
          if (e.key === 'ArrowUp') {
            e.preventDefault();
            stepParam(p.key, 1);
          } else if (e.key === 'ArrowDown') {
            e.preventDefault();
            stepParam(p.key, -1);
          } else if (e.key === 'Enter') {
            if (isLastField) {
              e.preventDefault();
              saveSetupForm(false);
            }
          }
        });

        controlsWrap.appendChild(minusBtn);
        controlsWrap.appendChild(input);
        controlsWrap.appendChild(plusBtn);
        controlsWrap.appendChild(diffSpan);

        row.appendChild(controlsWrap);
        rowsWrap.appendChild(row);
      });

      card.appendChild(rowsWrap);
      el.setupFormGroupsWrap.appendChild(card);
    });
  }

  function stepParam(key, direction) {
    const input = document.getElementById(`param-input-${key}`);
    if (!input) return;

    const type = input.dataset.type || 'int';
    const step = parseFloat(input.dataset.step || (type === 'float' ? '0.05' : '1'));
    const hasConfirmed = input.dataset.hasConfirmedRange === '1';
    const minVal = input.dataset.min !== undefined && input.dataset.min !== '' ? parseFloat(input.dataset.min) : null;
    const maxVal = input.dataset.max !== undefined && input.dataset.max !== '' ? parseFloat(input.dataset.max) : null;

    let currentVal = input.value.trim() !== '' ? parseFloat(input.value) : null;

    if (currentVal === null || isNaN(currentVal)) {
      if (currentActiveSetup && currentActiveSetup.params && currentActiveSetup.params[key] !== undefined) {
        currentVal = parseFloat(currentActiveSetup.params[key]);
      } else if (minVal !== null) {
        currentVal = minVal;
      } else {
        currentVal = 0;
      }
    } else {
      currentVal += direction * step;
    }

    if (hasConfirmed) {
      if (minVal !== null && currentVal < minVal) currentVal = minVal;
      if (maxVal !== null && currentVal > maxVal) currentVal = maxVal;
    }

    if (type === 'int') {
      input.value = Math.round(currentVal);
    } else {
      const decimals = step < 0.05 ? 3 : (step < 0.1 ? 2 : 1);
      input.value = (Math.round(currentVal * 1000) / 1000).toFixed(decimals);
    }

    onParamInputChanged(input);
  }

  function onParamInputChanged(input) {
    const key = input.dataset.key;
    const hasConfirmed = input.dataset.hasConfirmedRange === '1';
    const minVal = input.dataset.min !== undefined && input.dataset.min !== '' ? parseFloat(input.dataset.min) : null;
    const maxVal = input.dataset.max !== undefined && input.dataset.max !== '' ? parseFloat(input.dataset.max) : null;

    if (input.value.trim() !== '') {
      const val = parseFloat(input.value);
      if (hasConfirmed && !isNaN(val)) {
        if ((minVal !== null && val < minVal) || (maxVal !== null && val > maxVal)) {
          input.classList.add('out-of-bounds');
        } else {
          input.classList.remove('out-of-bounds');
        }
      } else {
        input.classList.remove('out-of-bounds');
      }
    } else {
      input.classList.remove('out-of-bounds');
    }

    updateSingleLiveDiff(key);
    scheduleSetupAutosave();
  }

  function updateSingleLiveDiff(key) {
    const input = document.getElementById(`param-input-${key}`);
    const badge = document.getElementById(`diff-badge-${key}`);
    if (!input || !badge) return;

    const currentCarVal = (currentActiveSetup && currentActiveSetup.params && currentActiveSetup.params[key] !== undefined)
      ? parseFloat(currentActiveSetup.params[key])
      : null;

    if (input.value.trim() === '' || currentCarVal === null || isNaN(currentCarVal)) {
      badge.textContent = '--';
      badge.className = 'diff-badge-indicator diff-na';
      return;
    }

    const inputVal = parseFloat(input.value);
    if (isNaN(inputVal)) {
      badge.textContent = '--';
      badge.className = 'diff-badge-indicator diff-na';
      return;
    }

    const delta = inputVal - currentCarVal;
    const type = input.dataset.type || 'int';

    if (Math.abs(delta) < 0.0001) {
      badge.textContent = '0';
      badge.className = 'diff-badge-indicator diff-zero';
    } else if (delta > 0) {
      const str = type === 'float' ? `+${delta.toFixed(2)}` : `+${Math.round(delta)}`;
      badge.textContent = str;
      badge.className = 'diff-badge-indicator diff-plus';
    } else {
      const str = type === 'float' ? `−${Math.abs(delta).toFixed(2)}` : `−${Math.abs(Math.round(delta))}`;
      badge.textContent = str;
      badge.className = 'diff-badge-indicator diff-minus';
    }
  }

  function updateLiveDiffs() {
    const inputs = document.querySelectorAll('.param-input');
    inputs.forEach(input => {
      updateSingleLiveDiff(input.dataset.key);
    });
  }

  function startFromBlank() {
    document.querySelectorAll('.param-input').forEach(input => {
      input.value = '';
      input.classList.remove('out-of-bounds');
    });
    if (el.btnStartBlank) el.btnStartBlank.classList.add('active');
    if (el.btnStartCopyCurrent) el.btnStartCopyCurrent.classList.remove('active');
    if (el.selectDuplicateSetup) el.selectDuplicateSetup.value = '';
    updateLiveDiffs();
    scheduleSetupAutosave();
  }

  function startFromCurrentCar() {
    if (!currentActiveSetup || !currentActiveSetup.params) {
      fetchCurrentSetup();
      alert('No active car setup captured yet. Complete a lap or drive out of pit lane in-game.');
      return;
    }

    const p = currentActiveSetup.params;
    document.querySelectorAll('.param-input').forEach(input => {
      const k = input.dataset.key;
      if (p[k] !== undefined && p[k] !== null) {
        input.value = p[k];
      } else {
        input.value = '';
      }
      input.classList.remove('out-of-bounds');
    });

    if (el.setupInputName && !el.setupInputName.value.trim()) {
      el.setupInputName.value = `Imported from Car Setup (${currentActiveSetup.id || 'Active'})`;
    }
    if (el.setupInputTrack && currentActiveSetup.track_id) {
      el.setupInputTrack.value = currentActiveSetup.track_id;
    }
    if (el.setupInputClass && currentActiveSetup.car_class !== undefined) {
      el.setupInputClass.value = currentActiveSetup.car_class;
    }

    if (el.btnStartBlank) el.btnStartBlank.classList.remove('active');
    if (el.btnStartCopyCurrent) el.btnStartCopyCurrent.classList.add('active');
    if (el.selectDuplicateSetup) el.selectDuplicateSetup.value = '';

    updateLiveDiffs();
    scheduleSetupAutosave();
  }

  function startFromDuplicate(setupId) {
    if (!setupId) return;
    const token = getToken();
    fetch(`/api/setups/${encodeURIComponent(setupId)}?token=${encodeURIComponent(token)}`)
      .then(res => res.json())
      .then(fullSetup => {
        populateFormFromSetup(fullSetup, true);
        if (el.btnStartBlank) el.btnStartBlank.classList.remove('active');
        if (el.btnStartCopyCurrent) el.btnStartCopyCurrent.classList.remove('active');
        updateLiveDiffs();
        scheduleSetupAutosave();
      })
      .catch(err => console.error('Error fetching duplicate setup:', err));
  }

  function populateFormFromSetup(setup, isDuplicate = false) {
    if (!setup) return;

    if (el.setupInputName) {
      el.setupInputName.value = isDuplicate ? `${setup.name || 'Setup'} (Copy)` : (setup.name || '');
    }
    if (el.setupInputClass && setup.car_class !== undefined) {
      el.setupInputClass.value = setup.car_class;
    }
    if (el.setupInputTrack && setup.track_id !== undefined) {
      el.setupInputTrack.value = setup.track_id;
    }
    if (el.setupInputConditions && setup.conditions) {
      el.setupInputConditions.value = setup.conditions;
    }
    if (el.setupInputSession && setup.session_type) {
      el.setupInputSession.value = setup.session_type;
    }
    if (el.setupInputTeam && setup.team_id !== undefined) {
      el.setupInputTeam.value = setup.team_id;
    }
    if (el.setupInputSourceNote) {
      el.setupInputSourceNote.value = isDuplicate ? `Duplicate of #${setup.id}` : (setup.source_note || '');
    }
    if (el.setupInputSourceLink) {
      el.setupInputSourceLink.value = setup.source_link || '';
    }
    if (el.setupInputNotes) {
      el.setupInputNotes.value = setup.notes || '';
    }

    const p = setup.params || {};
    document.querySelectorAll('.param-input').forEach(input => {
      const k = input.dataset.key;
      if (p[k] !== undefined && p[k] !== null) {
        input.value = p[k];
      } else {
        input.value = '';
      }
      input.classList.remove('out-of-bounds');
    });
  }

  function syncLiveMetaToForm() {
    if (el.setupsFilterTrack && el.setupInputTrack) {
      el.setupInputTrack.value = el.setupsFilterTrack.value;
    }
    if (el.setupsFilterClass && el.setupInputClass) {
      el.setupInputClass.value = el.setupsFilterClass.value;
    }
    if (el.setupsFilterConditions && el.setupInputConditions) {
      el.setupInputConditions.value = el.setupsFilterConditions.value;
    }
    if (el.setupsFilterSession && el.setupInputSession) {
      el.setupInputSession.value = el.setupsFilterSession.value;
    }
    if (currentActiveSetup) {
      if (currentActiveSetup.track_id && el.setupInputTrack) el.setupInputTrack.value = currentActiveSetup.track_id;
      if (currentActiveSetup.car_class !== undefined && el.setupInputClass) el.setupInputClass.value = currentActiveSetup.car_class;
      if (currentActiveSetup.team_id !== undefined && el.setupInputTeam) el.setupInputTeam.value = currentActiveSetup.team_id;
    }
  }

  function saveSetupForm(force = false) {
    const params = {};
    document.querySelectorAll('.param-input').forEach(input => {
      const valStr = input.value.trim();
      if (valStr !== '') {
        const type = input.dataset.type || 'int';
        params[input.dataset.key] = type === 'float' ? parseFloat(valStr) : parseInt(valStr, 10);
      }
    });

    const name = el.setupInputName ? el.setupInputName.value.trim() : '';
    const trackId = el.setupInputTrack ? parseInt(el.setupInputTrack.value, 10) : 7;
    const carClass = el.setupInputClass ? parseInt(el.setupInputClass.value, 10) : 0;
    const conditions = el.setupInputConditions ? el.setupInputConditions.value : 'dry';
    const sessionType = el.setupInputSession ? el.setupInputSession.value : 'race';
    const teamId = el.setupInputTeam && el.setupInputTeam.value.trim() !== '' ? parseInt(el.setupInputTeam.value, 10) : 0;
    const sourceNote = el.setupInputSourceNote ? el.setupInputSourceNote.value.trim() : '';
    const sourceLink = el.setupInputSourceLink ? el.setupInputSourceLink.value.trim() : '';
    const notes = el.setupInputNotes ? el.setupInputNotes.value.trim() : '';

    const token = getToken();

    if (setupFormMode === 'edit' && setupFormEditingSetup) {
      const isCaptured = setupFormEditingSetup.source === 'captured';
      const payload = {
        name: name || setupFormEditingSetup.name,
        track_id: trackId,
        car_class: carClass,
        conditions: conditions,
        session_type: sessionType,
        team_id: teamId,
        source_note: sourceNote,
        source_link: sourceLink,
        notes: notes,
        params: params,
        allow_fork: isCaptured,
      };

      fetch(`/api/setups/${encodeURIComponent(setupFormEditingSetup.id)}?token=${encodeURIComponent(token)}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      })
        .then(res => res.json())
        .then(data => {
          if (data.detail) {
            alert('Save error: ' + JSON.stringify(data.detail));
            return;
          }
          clearSetupDraft();
          closeSetupFormModal();
          alert(isCaptured ? `Forked captured setup into new imported setup #${data.id}!` : `Setup #${data.id} updated successfully!`);
          fetchSetups(false);
        })
        .catch(err => alert('Save error: ' + err));
    } else {
      const payload = {
        name: name || 'Custom Setup',
        track_id: trackId,
        car_class: carClass,
        conditions: conditions,
        session_type: sessionType,
        team_id: teamId,
        source: 'imported',
        source_note: sourceNote,
        source_link: sourceLink,
        notes: notes,
        params: params,
        force: force,
      };

      fetch(`/api/setups?token=${encodeURIComponent(token)}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      })
        .then(res => {
          if (res.status === 409) {
            return res.json().then(dup => {
              handleDuplicateSetup(dup, payload);
            });
          }
          return res.json().then(data => {
            if (data.detail) {
              alert('Save error: ' + JSON.stringify(data.detail));
              return;
            }
            clearSetupDraft();
            closeSetupFormModal();
            closeDuplicateModal();
            alert(`Setup saved successfully! (ID: ${data.id})`);
            fetchSetups(false);
          });
        })
        .catch(err => alert('Save error: ' + err));
    }
  }

  function handleDuplicateSetup(dup, payload) {
    lastDuplicateData = { dup, payload };
    if (el.dupModalFp) el.dupModalFp.textContent = dup.fingerprint || '--';
    const ex = dup.existing_setup || {};
    if (el.dupExistingName) el.dupExistingName.textContent = ex.name || `Setup #${ex.id}`;
    if (el.dupExistingMeta) {
      el.dupExistingMeta.textContent = `ID #${ex.id} | Source: ${ex.source} | Conditions: ${(ex.conditions || 'dry').toUpperCase()}`;
    }
    if (el.setupDuplicateModal) el.setupDuplicateModal.classList.remove('hidden');
  }

  function openDuplicateExisting() {
    if (lastDuplicateData && lastDuplicateData.dup && lastDuplicateData.dup.existing_setup) {
      const id = lastDuplicateData.dup.existing_setup.id;
      closeDuplicateModal();
      closeSetupFormModal();
      openSetupDetails(id);
    }
  }

  function closeDuplicateModal() {
    if (el.setupDuplicateModal) el.setupDuplicateModal.classList.add('hidden');
  }

  function deleteSetupItem(id, name) {
    if (!confirm(`Are you sure you want to delete setup "${name || id}" (ID: ${id})?`)) {
      return;
    }
    const token = getToken();
    fetch(`/api/setups/${encodeURIComponent(id)}?token=${encodeURIComponent(token)}`, {
      method: 'DELETE'
    })
      .then(res => {
        if (!res.ok) {
          return res.json().then(e => { throw new Error(e.detail || `HTTP ${res.status}`); });
        }
        return res.json();
      })
      .then(() => {
        alert(`Setup #${id} deleted.`);
        fetchSetups(false);
      })
      .catch(err => alert('Delete error: ' + err.message));
  }

  function scheduleSetupAutosave() {
    if (setupAutosaveTimer) clearTimeout(setupAutosaveTimer);
    setupAutosaveTimer = setTimeout(() => {
      saveSetupDraft();
    }, 500);
  }

  function saveSetupDraft() {
    const params = {};
    document.querySelectorAll('.param-input').forEach(input => {
      if (input.value.trim() !== '') {
        params[input.dataset.key] = input.value.trim();
      }
    });

    const draft = {
      name: el.setupInputName ? el.setupInputName.value : '',
      car_class: el.setupInputClass ? el.setupInputClass.value : '0',
      track_id: el.setupInputTrack ? el.setupInputTrack.value : '7',
      conditions: el.setupInputConditions ? el.setupInputConditions.value : 'dry',
      session_type: el.setupInputSession ? el.setupInputSession.value : 'race',
      team_id: el.setupInputTeam ? el.setupInputTeam.value : '0',
      source_note: el.setupInputSourceNote ? el.setupInputSourceNote.value : '',
      source_link: el.setupInputSourceLink ? el.setupInputSourceLink.value : '',
      notes: el.setupInputNotes ? el.setupInputNotes.value : '',
      params: params,
      time: new Date().toLocaleTimeString(),
    };

    try {
      localStorage.setItem(SETUP_DRAFT_KEY, JSON.stringify(draft));
      if (el.setupDraftStatus) {
        el.setupDraftStatus.textContent = `Draft autosaved (${draft.time})`;
        el.setupDraftStatus.style.color = 'var(--text-muted)';
      }
    } catch (e) {
      console.debug('Autosave error:', e);
    }
  }

  function restoreSetupDraft() {
    try {
      const raw = localStorage.getItem(SETUP_DRAFT_KEY);
      if (!raw) return false;
      const draft = JSON.parse(raw);
      if (!draft || typeof draft !== 'object') return false;

      if (el.setupInputName && draft.name) el.setupInputName.value = draft.name;
      if (el.setupInputClass && draft.car_class) el.setupInputClass.value = draft.car_class;
      if (el.setupInputTrack && draft.track_id) el.setupInputTrack.value = draft.track_id;
      if (el.setupInputConditions && draft.conditions) el.setupInputConditions.value = draft.conditions;
      if (el.setupInputSession && draft.session_type) el.setupInputSession.value = draft.session_type;
      if (el.setupInputTeam && draft.team_id) el.setupInputTeam.value = draft.team_id;
      if (el.setupInputSourceNote && draft.source_note) el.setupInputSourceNote.value = draft.source_note;
      if (el.setupInputSourceLink && draft.source_link) el.setupInputSourceLink.value = draft.source_link;
      if (el.setupInputNotes && draft.notes) el.setupInputNotes.value = draft.notes;

      const p = draft.params || {};
      document.querySelectorAll('.param-input').forEach(input => {
        const k = input.dataset.key;
        if (p[k] !== undefined) {
          input.value = p[k];
        }
      });

      if (el.setupDraftStatus) {
        el.setupDraftStatus.textContent = `Restored draft (${draft.time || 'earlier'})`;
        el.setupDraftStatus.style.color = 'var(--color-cyan)';
      }
      return true;
    } catch (e) {
      return false;
    }
  }

  function clearSetupDraft() {
    localStorage.removeItem(SETUP_DRAFT_KEY);
    if (el.setupDraftStatus) {
      el.setupDraftStatus.textContent = 'Draft cleared';
      el.setupDraftStatus.style.color = 'var(--text-muted)';
    }
  }

  // ==========================================================================
  // Track Notes Editor Module
  // ==========================================================================
  let loadedTracksList = [];
  let currentSelectedTrackSlug = null;
  let currentTrackEditorMode = 'form'; // 'form' | 'raw'

  function initTracksTab() {
    if (el.tracksSearchInput) {
      el.tracksSearchInput.addEventListener('input', (e) => {
        filterTracksList(e.target.value.trim().toLowerCase());
      });
    }

    if (el.btnTrackModeForm) {
      el.btnTrackModeForm.addEventListener('click', () => setTrackEditorMode('form'));
    }

    if (el.btnTrackModeRaw) {
      el.btnTrackModeRaw.addEventListener('click', () => setTrackEditorMode('raw'));
    }

    if (el.btnAddCornerRow) {
      el.btnAddCornerRow.addEventListener('click', () => addCornerRow());
    }

    if (el.btnSaveTrackNotes) {
      el.btnSaveTrackNotes.addEventListener('click', saveCurrentTrackNotes);
    }
  }

  function fetchTracksList() {
    const token = getToken();
    fetch(`/api/tracks?token=${encodeURIComponent(token)}`)
      .then(res => res.json())
      .then(data => {
        loadedTracksList = data.tracks || [];
        if (el.tracksCountBadge) {
          el.tracksCountBadge.textContent = `${loadedTracksList.length} circuits`;
        }
        renderTracksList(loadedTracksList);

        if (!currentSelectedTrackSlug && loadedTracksList.length > 0) {
          const silverstone = loadedTracksList.find(t => t.slug === 'silverstone');
          selectTrack(silverstone ? silverstone.slug : loadedTracksList[0].slug);
        }
      })
      .catch(err => console.error('Error fetching tracks:', err));
  }

  function renderTracksList(tracks) {
    if (!el.tracksListContainer) return;
    el.tracksListContainer.innerHTML = '';

    tracks.forEach(t => {
      const div = document.createElement('div');
      div.className = `track-item-card ${t.slug === currentSelectedTrackSlug ? 'active' : ''}`;
      div.dataset.slug = t.slug;

      div.innerHTML = `
        <div class="track-item-main">
          <span class="track-name-label">${t.name}</span>
          <span class="track-item-badge ${t.has_file ? 'has-notes' : 'no-notes'}">
            ${t.has_file ? `${t.corners_count} corners` : 'no notes'}
          </span>
        </div>
        <div class="track-item-sub mono">${t.slug}.md</div>
      `;

      div.addEventListener('click', () => selectTrack(t.slug));
      el.tracksListContainer.appendChild(div);
    });
  }

  function filterTracksList(query) {
    if (!query) {
      renderTracksList(loadedTracksList);
      return;
    }
    const filtered = loadedTracksList.filter(t => 
      t.name.toLowerCase().includes(query) || t.slug.toLowerCase().includes(query)
    );
    renderTracksList(filtered);
  }

  function selectTrack(slug) {
    currentSelectedTrackSlug = slug;

    document.querySelectorAll('.track-item-card').forEach(item => {
      if (item.dataset.slug === slug) {
        item.classList.add('active');
      } else {
        item.classList.remove('active');
      }
    });

    const token = getToken();
    fetch(`/api/tracks/${encodeURIComponent(slug)}?token=${encodeURIComponent(token)}`)
      .then(res => res.json())
      .then(data => {
        if (el.trackEditorHeading) el.trackEditorHeading.textContent = data.name.toUpperCase();
        if (el.trackEditorFileBadge) el.trackEditorFileBadge.textContent = `knowledge/tracks/${data.slug}.md`;

        populateCornersTable(data.corners_list || []);
        if (el.trackSectionOvertaking) el.trackSectionOvertaking.value = data.sections.overtaking || '';
        if (el.trackSectionTyres) el.trackSectionTyres.value = data.sections.tyres || '';
        if (el.trackSectionSetupNotes) el.trackSectionSetupNotes.value = data.sections.setup_notes || '';

        if (el.trackRawContent) el.trackRawContent.value = data.raw_markdown || '';

        if (el.trackSaveStatus) {
          el.trackSaveStatus.textContent = 'Ready';
          el.trackSaveStatus.className = 'track-status-pill';
        }
      })
      .catch(err => console.error('Error fetching track notes:', err));
  }

  function populateCornersTable(cornersList) {
    if (!el.trackCornersTbody) return;
    el.trackCornersTbody.innerHTML = '';

    if (cornersList.length === 0) {
      addCornerRow({ number: 1, name: '', description: '' });
      return;
    }

    cornersList.forEach(c => addCornerRow(c));
  }

  function addCornerRow(corner = null) {
    if (!el.trackCornersTbody) return;

    let nextNum = 1;
    const existingRows = el.trackCornersTbody.querySelectorAll('.corner-row');
    if (existingRows.length > 0) {
      const lastNumInput = existingRows[existingRows.length - 1].querySelector('.corner-num-input');
      if (lastNumInput && !isNaN(parseInt(lastNumInput.value, 10))) {
        nextNum = parseInt(lastNumInput.value, 10) + 1;
      }
    }

    const tr = document.createElement('tr');
    tr.className = 'corner-row';

    tr.innerHTML = `
      <td>
        <input type="number" class="f1-input corner-num-input" value="${corner ? corner.number : nextNum}" min="1" max="99">
      </td>
      <td>
        <input type="text" class="f1-input corner-name-input" value="${corner ? (corner.name || '') : ''}" placeholder="e.g. Copse (T9)">
      </td>
      <td>
        <input type="text" class="f1-input corner-desc-input" value="${corner ? (corner.description || '') : ''}" placeholder="e.g. Fast right, flat in qualifying">
      </td>
      <td style="text-align: center;">
        <button type="button" class="btn-del-corner" title="Remove corner">✕</button>
      </td>
    `;

    tr.querySelector('.btn-del-corner').addEventListener('click', () => {
      tr.remove();
    });

    el.trackCornersTbody.appendChild(tr);
  }

  function getCornersListFromTable() {
    const corners = [];
    if (!el.trackCornersTbody) return corners;

    const rows = el.trackCornersTbody.querySelectorAll('.corner-row');
    rows.forEach(row => {
      const numInput = row.querySelector('.corner-num-input');
      const nameInput = row.querySelector('.corner-name-input');
      const descInput = row.querySelector('.corner-desc-input');

      const number = numInput ? parseInt(numInput.value, 10) : 0;
      const name = nameInput ? nameInput.value.trim() : '';
      const description = descInput ? descInput.value.trim() : '';

      if (name || description) {
        corners.push({ number, name, description });
      }
    });

    return corners;
  }

  function buildMarkdownFromForm() {
    const trackName = el.trackEditorHeading ? el.trackEditorHeading.textContent : 'Track Notes';
    const corners = getCornersListFromTable();
    const overtaking = el.trackSectionOvertaking ? el.trackSectionOvertaking.value.trim() : '';
    const tyres = el.trackSectionTyres ? el.trackSectionTyres.value.trim() : '';
    const setupNotes = el.trackSectionSetupNotes ? el.trackSectionSetupNotes.value.trim() : '';

    let md = `# ${trackName}\n\n## Corners\n`;
    if (corners.length > 0) {
      corners.forEach(c => {
        md += `${c.number}. ${c.name}: ${c.description}\n`;
      });
    } else {
      md += `(No corners defined yet)\n`;
    }

    md += `\n## Overtaking\n${overtaking || 'None.'}\n`;
    md += `\n## Tyres\n${tyres || 'None.'}\n`;
    md += `\n## Setup notes\n${setupNotes || 'None.'}\n`;

    return md;
  }

  function parseMarkdownToForm(raw) {
    const sections = {
      corners: '',
      overtaking: '',
      tyres: '',
      setup_notes: ''
    };

    const lines = raw.split('\n');
    let currentSec = null;
    const cornersList = [];

    for (let line of lines) {
      const trimmed = line.trim();
      if (trimmed.startsWith('## Corners')) {
        currentSec = 'corners';
        continue;
      } else if (trimmed.startsWith('## Overtaking')) {
        currentSec = 'overtaking';
        continue;
      } else if (trimmed.startsWith('## Tyres')) {
        currentSec = 'tyres';
        continue;
      } else if (trimmed.startsWith('## Setup notes')) {
        currentSec = 'setup_notes';
        continue;
      } else if (trimmed.startsWith('## ')) {
        currentSec = null;
        continue;
      }

      if (currentSec === 'corners') {
        const match = trimmed.match(/^(\d+)\.\s*([^:]+):\s*(.*)$/);
        if (match) {
          cornersList.push({
            number: parseInt(match[1], 10),
            name: match[2].trim(),
            description: match[3].trim()
          });
        }
      } else if (currentSec && sections[currentSec] !== undefined) {
        sections[currentSec] += line + '\n';
      }
    }

    populateCornersTable(cornersList);
    if (el.trackSectionOvertaking) el.trackSectionOvertaking.value = sections.overtaking.trim();
    if (el.trackSectionTyres) el.trackSectionTyres.value = sections.tyres.trim();
    if (el.trackSectionSetupNotes) el.trackSectionSetupNotes.value = sections.setup_notes.trim();
  }

  function setTrackEditorMode(mode) {
    currentTrackEditorMode = mode;

    if (mode === 'raw') {
      if (el.btnTrackModeRaw) el.btnTrackModeRaw.classList.add('active');
      if (el.btnTrackModeForm) el.btnTrackModeForm.classList.remove('active');
      if (el.trackEditorRawView) el.trackEditorRawView.classList.remove('hidden');
      if (el.trackEditorFormView) el.trackEditorFormView.classList.add('hidden');
      if (el.trackRawContent) {
        el.trackRawContent.value = buildMarkdownFromForm();
      }
    } else {
      if (el.btnTrackModeForm) el.btnTrackModeForm.classList.add('active');
      if (el.btnTrackModeRaw) el.btnTrackModeRaw.classList.remove('active');
      if (el.trackEditorFormView) el.trackEditorFormView.classList.remove('hidden');
      if (el.trackEditorRawView) el.trackEditorRawView.classList.add('hidden');
      if (el.trackRawContent) {
        parseMarkdownToForm(el.trackRawContent.value);
      }
    }
  }

  function saveCurrentTrackNotes() {
    if (!currentSelectedTrackSlug) {
      alert('Please select a track first.');
      return;
    }

    if (el.trackSaveStatus) {
      el.trackSaveStatus.textContent = 'Saving...';
      el.trackSaveStatus.className = 'track-status-pill saving';
    }

    const token = getToken();
    let payload = {};

    if (currentTrackEditorMode === 'raw') {
      payload = {
        raw_markdown: el.trackRawContent ? el.trackRawContent.value : ''
      };
    } else {
      const corners = getCornersListFromTable();
      payload = {
        sections: {
          overtaking: el.trackSectionOvertaking ? el.trackSectionOvertaking.value.trim() : '',
          tyres: el.trackSectionTyres ? el.trackSectionTyres.value.trim() : '',
          setup_notes: el.trackSectionSetupNotes ? el.trackSectionSetupNotes.value.trim() : '',
        },
        corners_list: corners
      };
    }

    fetch(`/api/tracks/${encodeURIComponent(currentSelectedTrackSlug)}?token=${encodeURIComponent(token)}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    })
      .then(res => res.json())
      .then(data => {
        if (data.detail) {
          alert('Save error: ' + JSON.stringify(data.detail));
          if (el.trackSaveStatus) el.trackSaveStatus.textContent = 'Error';
          return;
        }
        if (el.trackSaveStatus) {
          el.trackSaveStatus.textContent = `Saved & reloaded (${data.corners_count} corners)`;
          el.trackSaveStatus.className = 'track-status-pill saved';
        }
        fetchTracksList();
      })
      .catch(err => {
        alert('Save error: ' + err);
        if (el.trackSaveStatus) el.trackSaveStatus.textContent = 'Error';
      });
  }

  // --- Engineer Persona & Settings ---
  async function fetchPersonaSettings() {
    try {
      const token = getToken();
      const url = token ? `/api/settings/persona?token=${encodeURIComponent(token)}` : '/api/settings/persona';
      const res = await fetch(url);
      if (!res.ok) return;
      const data = await res.json();
      if (el.settingPersonaName && data.name) el.settingPersonaName.value = data.name;
      if (el.settingProfanity && data.profanity) el.settingProfanity.value = data.profanity;
      if (el.settingReactionFreq && data.reaction_frequency) el.settingReactionFreq.value = data.reaction_frequency;
      if (el.settingMinGapS && data.min_gap_s != null) el.settingMinGapS.value = data.min_gap_s;
    } catch (e) {
      console.warn("Could not fetch persona settings:", e);
    }
  }

  async function savePersonaSettings() {
    try {
      const token = getToken();
      const payload = {
        name: el.settingPersonaName ? el.settingPersonaName.value : 'cocky',
        profanity: el.settingProfanity ? el.settingProfanity.value : 'mild',
        reaction_frequency: el.settingReactionFreq ? el.settingReactionFreq.value : 'normal',
        min_gap_s: el.settingMinGapS ? parseFloat(el.settingMinGapS.value) : 20,
      };
      const url = token ? `/api/settings/persona?token=${encodeURIComponent(token)}` : '/api/settings/persona';
      const res = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      if (res.ok) {
        if (el.settingsFeedbackMsg) {
          el.settingsFeedbackMsg.style.display = 'inline';
          setTimeout(() => {
            if (el.settingsFeedbackMsg) el.settingsFeedbackMsg.style.display = 'none';
          }, 3000);
        }
      }
    } catch (e) {
      console.error("Could not save persona settings:", e);
    }
  }

  // --- App Sound Test & Audio Hardware ---
  let isMicRecording = false;
  let micLevelPollInterval = null;
  let isSoundTestBlocked = false;

  function updateSoundTestSessionState(isLive) {
    isSoundTestBlocked = !!isLive;
    if (el.soundTestSessionBanner) {
      if (isSoundTestBlocked) {
        el.soundTestSessionBanner.classList.remove('hidden');
        if (el.soundTestControlsContainer) {
          el.soundTestControlsContainer.style.opacity = '0.45';
          el.soundTestControlsContainer.style.pointerEvents = 'none';
        }
        if (el.soundTestStatusBadge) {
          el.soundTestStatusBadge.textContent = 'SESSION LIVE (BLOCKED)';
          el.soundTestStatusBadge.style.color = '#ef4444';
          el.soundTestStatusBadge.style.borderColor = '#ef4444';
          el.soundTestStatusBadge.style.background = 'rgba(239, 68, 68, 0.15)';
        }
      } else {
        el.soundTestSessionBanner.classList.add('hidden');
        if (el.soundTestControlsContainer) {
          el.soundTestControlsContainer.style.opacity = '1';
          el.soundTestControlsContainer.style.pointerEvents = 'auto';
        }
        if (el.soundTestStatusBadge) {
          el.soundTestStatusBadge.textContent = 'OFFLINE READY';
          el.soundTestStatusBadge.style.color = '#00e5ff';
          el.soundTestStatusBadge.style.borderColor = '#00e5ff';
          el.soundTestStatusBadge.style.background = 'rgba(0, 229, 255, 0.15)';
        }
      }
    }
  }

  async function fetchAudioDevices() {
    try {
      const token = getToken();
      const url = token ? `/api/audio/devices?token=${encodeURIComponent(token)}` : '/api/audio/devices';
      const res = await fetch(url);
      if (!res.ok) return;
      const data = await res.json();

      if (el.soundTestDeviceWarning) {
        if (data.warning) {
          el.soundTestDeviceWarning.textContent = '⚠️ ' + data.warning;
          el.soundTestDeviceWarning.classList.remove('hidden');
        } else {
          el.soundTestDeviceWarning.classList.add('hidden');
        }
      }

      if (el.selectAudioInput && Array.isArray(data.input_devices)) {
        el.selectAudioInput.innerHTML = '<option value="">(System Default)</option>';
        data.input_devices.forEach(dev => {
          const opt = document.createElement('option');
          opt.value = dev.id;
          opt.textContent = `${dev.id}: ${dev.name} (${dev.hostapi || 'Direct'})`;
          if (data.current_input != null && dev.id === data.current_input) {
            opt.selected = true;
          }
          el.selectAudioInput.appendChild(opt);
        });
      }

      if (el.selectAudioOutput && Array.isArray(data.output_devices)) {
        el.selectAudioOutput.innerHTML = '<option value="">(System Default)</option>';
        data.output_devices.forEach(dev => {
          const opt = document.createElement('option');
          opt.value = dev.id;
          opt.textContent = `${dev.id}: ${dev.name} (${dev.hostapi || 'Direct'})`;
          if (data.current_output != null && dev.id === data.current_output) {
            opt.selected = true;
          }
          el.selectAudioOutput.appendChild(opt);
        });
      }
    } catch (e) {
      console.warn("Could not fetch audio devices:", e);
    }
  }

  async function saveAudioDevices() {
    try {
      const token = getToken();
      const inVal = el.selectAudioInput ? el.selectAudioInput.value : '';
      const outVal = el.selectAudioOutput ? el.selectAudioOutput.value : '';
      const payload = {
        input_device: inVal !== '' ? parseInt(inVal, 10) : null,
        output_device: outVal !== '' ? parseInt(outVal, 10) : null,
      };
      const url = token ? `/api/audio/devices?token=${encodeURIComponent(token)}` : '/api/audio/devices';
      const res = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      if (res.ok) {
        const data = await res.json();
        if (el.soundTestDeviceWarning) {
          if (data.warning) {
            el.soundTestDeviceWarning.textContent = '⚠️ ' + data.warning;
            el.soundTestDeviceWarning.classList.remove('hidden');
          } else {
            el.soundTestDeviceWarning.classList.add('hidden');
          }
        }
      }
    } catch (e) {
      console.error("Could not save audio devices:", e);
    }
  }

  async function fetchVolumes() {
    try {
      const token = getToken();
      const url = token ? `/api/settings/volumes?token=${encodeURIComponent(token)}` : '/api/settings/volumes';
      const res = await fetch(url);
      if (!res.ok) return;
      const data = await res.json();
      if (el.sliderVolEngineer && data.engineer != null) {
        const pct = Math.round(data.engineer * 100);
        el.sliderVolEngineer.value = pct;
        if (el.labelVolEngineer) el.labelVolEngineer.textContent = pct + '%';
      }
      if (el.sliderVolSpotter && data.spotter != null) {
        const pct = Math.round(data.spotter * 100);
        el.sliderVolSpotter.value = pct;
        if (el.labelVolSpotter) el.labelVolSpotter.textContent = pct + '%';
      }
      if (el.sliderVolBeeps && data.menu_beeps != null) {
        const pct = Math.round(data.menu_beeps * 100);
        el.sliderVolBeeps.value = pct;
        if (el.labelVolBeeps) el.labelVolBeeps.textContent = pct + '%';
      }
    } catch (e) {
      console.warn("Could not fetch volumes:", e);
    }
  }

  async function saveVolumes() {
    try {
      const token = getToken();
      const payload = {
        engineer: el.sliderVolEngineer ? parseInt(el.sliderVolEngineer.value, 10) / 100.0 : 1.0,
        spotter: el.sliderVolSpotter ? parseInt(el.sliderVolSpotter.value, 10) / 100.0 : 0.9,
        menu_beeps: el.sliderVolBeeps ? parseInt(el.sliderVolBeeps.value, 10) / 100.0 : 0.7,
      };
      const url = token ? `/api/settings/volumes?token=${encodeURIComponent(token)}` : '/api/settings/volumes';
      await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
    } catch (e) {
      console.error("Could not save volumes:", e);
    }
  }

  async function runSpeakerTest() {
    if (isSoundTestBlocked) return;
    try {
      const token = getToken();
      if (el.btnSpeakerTest) {
        el.btnSpeakerTest.disabled = true;
        el.btnSpeakerTest.textContent = '🔊 PLAYING...';
      }
      if (el.speakerTestStatus) el.speakerTestStatus.textContent = 'Playing test audio through output device...';

      const payload = {
        with_radio_fx: el.toggleSpeakerFx ? el.toggleSpeakerFx.checked : true,
      };
      const url = token ? `/api/sound_test/speaker?token=${encodeURIComponent(token)}` : '/api/sound_test/speaker';
      const res = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      if (res.status === 409) {
        if (el.speakerTestStatus) el.speakerTestStatus.textContent = 'Blocked: session is currently live.';
      } else if (res.ok) {
        if (el.speakerTestStatus) el.speakerTestStatus.textContent = 'Speaker test completed.';
      }
    } catch (e) {
      if (el.speakerTestStatus) el.speakerTestStatus.textContent = 'Speaker test error: ' + e;
    } finally {
      if (el.btnSpeakerTest) {
        el.btnSpeakerTest.disabled = false;
        el.btnSpeakerTest.textContent = '🔊 PLAY SPEAKER TEST';
      }
    }
  }

  async function startMicRecording() {
    if (isSoundTestBlocked || isMicRecording) return;
    isMicRecording = true;
    try {
      const token = getToken();
      if (el.btnMicTest) {
        el.btnMicTest.classList.add('btn-mic-recording');
        el.btnMicTest.textContent = '🔴 RECORDING... RELEASE TO PLAY';
      }
      if (el.micTranscriptResult) el.micTranscriptResult.textContent = 'Listening... Speak clearly into your mic.';
      if (el.micClippingBadge) el.micClippingBadge.classList.add('hidden');

      const url = token ? `/api/sound_test/mic/start?token=${encodeURIComponent(token)}` : '/api/sound_test/mic/start';
      await fetch(url, { method: 'POST' });

      if (micLevelPollInterval) clearInterval(micLevelPollInterval);
      micLevelPollInterval = setInterval(pollMicLevel, 70);
    } catch (e) {
      console.error("Could not start mic test:", e);
    }
  }

  async function pollMicLevel() {
    if (!isMicRecording) return;
    try {
      const token = getToken();
      const url = token ? `/api/sound_test/mic/level?token=${encodeURIComponent(token)}` : '/api/sound_test/mic/level';
      const res = await fetch(url);
      if (!res.ok) return;
      const data = await res.json();

      const peakPct = Math.min(100, Math.max(0, (data.peak || 0) * 100));
      if (el.micMeterFill) {
        el.micMeterFill.style.width = peakPct.toFixed(1) + '%';
        if (data.clipping) {
          el.micMeterFill.style.backgroundColor = '#ef4444';
        } else if (data.peak > 0.8) {
          el.micMeterFill.style.backgroundColor = '#f59e0b';
        } else {
          el.micMeterFill.style.backgroundColor = '#00e676';
        }
      }
      if (el.micMeterDb) {
        el.micMeterDb.textContent = (data.peak_db !== undefined && isFinite(data.peak_db)) ? `${data.peak_db.toFixed(1)} dB` : '-∞ dB';
      }
      if (el.micMetricsLabel) {
        el.micMetricsLabel.textContent = `Peak: ${(data.peak || 0).toFixed(2)} | RMS: ${(data.rms || 0).toFixed(2)}`;
      }
      if (el.micClippingBadge) {
        if (data.clipping) {
          el.micClippingBadge.classList.remove('hidden');
        }
      }
    } catch (e) {
      // ignore
    }
  }

  async function stopMicRecording() {
    if (!isMicRecording) return;
    isMicRecording = false;
    if (micLevelPollInterval) {
      clearInterval(micLevelPollInterval);
      micLevelPollInterval = null;
    }
    if (el.btnMicTest) {
      el.btnMicTest.classList.remove('btn-mic-recording');
      el.btnMicTest.textContent = '⏳ PROCESSING & PLAYING BACK...';
    }

    try {
      const token = getToken();
      const url = token ? `/api/sound_test/mic/stop?token=${encodeURIComponent(token)}` : '/api/sound_test/mic/stop';
      const res = await fetch(url, { method: 'POST' });
      if (res.ok) {
        const data = await res.json();
        if (el.micTranscriptResult) {
          const t = data.transcript && data.transcript.trim() ? `"${data.transcript}"` : '(No speech detected)';
          el.micTranscriptResult.textContent = `Transcribed: ${t} • Peak: ${(data.peak || 0).toFixed(2)} (${(data.peak_db || 0).toFixed(1)} dB)`;
        }
        if (el.micClippingBadge) {
          if (data.clipping) {
            el.micClippingBadge.classList.remove('hidden');
          } else {
            el.micClippingBadge.classList.add('hidden');
          }
        }
      }
    } catch (e) {
      console.error("Could not stop mic test:", e);
      if (el.micTranscriptResult) el.micTranscriptResult.textContent = 'Error processing mic test.';
    } finally {
      setTimeout(() => {
        if (el.btnMicTest && !isMicRecording) {
          el.btnMicTest.textContent = '🎤 HOLD TO RECORD MIC';
        }
      }, 1500);
    }
  }

  async function triggerInGameRadioCheck() {
    try {
      const token = getToken();
      if (el.btnTriggerRadioCheck) {
        el.btnTriggerRadioCheck.disabled = true;
        el.btnTriggerRadioCheck.textContent = '📻 CHECKING...';
      }
      const url = token ? `/api/radio_check?token=${encodeURIComponent(token)}` : '/api/radio_check';
      const res = await fetch(url, { method: 'POST' });
      const data = await res.json();
      console.log("[Radio Check] Triggered:", data);
    } catch (e) {
      console.error("Could not trigger radio check:", e);
    } finally {
      setTimeout(() => {
        if (el.btnTriggerRadioCheck) {
          el.btnTriggerRadioCheck.disabled = false;
          el.btnTriggerRadioCheck.textContent = '📻 TRIGGER RADIO CHECK';
        }
      }, 3000);
    }
  }

  // --- Network & Ports Settings ---
  async function fetchPortSettings() {
    try {
      const token = getToken();
      const url = token ? `/api/settings/ports?token=${encodeURIComponent(token)}` : '/api/settings/ports';
      const res = await fetch(url);
      if (!res.ok) return;
      const data = await res.json();
      if (el.settingTelemetryPort && data.telemetry_port != null) {
        el.settingTelemetryPort.value = data.telemetry_port;
      }
      if (el.settingDashboardPort && data.dashboard_port != null) {
        el.settingDashboardPort.value = data.dashboard_port;
      }
      if (el.settingAskPort && data.ask_port !== undefined) {
        el.settingAskPort.checked = !!data.ask_port;
      }
    } catch (e) {
      console.warn("Could not fetch port settings:", e);
    }
  }

  async function savePortSettings() {
    try {
      const token = getToken();
      const payload = {
        telemetry_port: el.settingTelemetryPort ? parseInt(el.settingTelemetryPort.value, 10) : 20777,
        dashboard_port: el.settingDashboardPort ? parseInt(el.settingDashboardPort.value, 10) : 8765,
        ask_port: el.settingAskPort ? el.settingAskPort.checked : false,
      };
      const url = token ? `/api/settings/ports?token=${encodeURIComponent(token)}` : '/api/settings/ports';
      const res = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      if (res.ok) {
        if (el.portsFeedbackMsg) {
          el.portsFeedbackMsg.style.display = 'inline';
          setTimeout(() => {
            if (el.portsFeedbackMsg) el.portsFeedbackMsg.style.display = 'none';
          }, 3000);
        }
      }
    } catch (e) {
      console.error("Could not save port settings:", e);
    }
  }

  // --- AI Usage Guard Status ---
  async function fetchUsageStatus() {
    try {
      const token = getToken();
      const url = token ? `/api/usage?token=${encodeURIComponent(token)}` : '/api/usage';
      const res = await fetch(url);
      if (!res.ok) return;
      const data = await res.json();

      if (el.usageLastProviderBadge) {
        el.usageLastProviderBadge.textContent = (data.last_provider || 'NONE').toUpperCase();
      }

      if (el.usageResetCountdown && data.countdown_str) {
        el.usageResetCountdown.textContent = `RESET IN: ${data.countdown_str} (00:00 UTC)`;
      }

      if (data.overall_quota_pct !== undefined) {
        updateCloudQuotaPill({ pct: data.overall_quota_pct, cutoff_pct: data.cutoff_pct });
      }

      if (!el.usageCardsContainer) return;
      el.usageCardsContainer.innerHTML = '';

      const services = data.services || {};
      for (const [name, s] of Object.entries(services)) {
        const card = document.createElement('div');
        card.className = 'usage-card';

        const stateClass = (s.state || 'ok').replace(/\s+/g, '-');
        const stateText = (s.state || 'ok').toUpperCase();

        let pauseHtml = '';
        if (s.paused_remaining_s > 0) {
          pauseHtml = `
            <div class="usage-pause-alert">
              <span>⚠️ Paused for ${s.paused_remaining_s}s: ${s.pause_reason || 'Rate limited'}</span>
            </div>
          `;
        }

        function renderBar(label, current, limit, unit, pct) {
          if (!limit || limit <= 0) return '';
          const p = Math.min(100, Math.max(0, pct || 0));
          const fillClass = p >= 80 ? 'fill-red' : (p >= 50 ? 'fill-yellow' : 'fill-green');
          return `
            <div class="usage-metric-item">
              <div class="usage-metric-header">
                <span>${label}</span>
                <span class="usage-metric-val">${Number(current).toLocaleString()} / ${Number(limit).toLocaleString()} ${unit} (${p.toFixed(1)}%)</span>
              </div>
              <div class="usage-progress-track">
                <div class="usage-progress-fill ${fillClass}" style="width: ${p}%"></div>
              </div>
            </div>
          `;
        }

        let metricsHtml = '<div class="usage-metrics-group">';
        if (s.minute && s.daily) {
          metricsHtml += renderBar('Minute Requests (RPM)', s.minute.requests, s.minute.requests_limit, 'RPM', s.minute.requests_pct);
          metricsHtml += renderBar('Today Requests (RPD)', s.daily.requests, s.daily.requests_limit, 'RPD', s.daily.requests_pct);

          if (s.minute.tokens_limit > 0 || s.daily.tokens_limit > 0) {
            metricsHtml += renderBar('Minute Tokens (TPM)', s.minute.tokens, s.minute.tokens_limit, 'TPM', s.minute.tokens_pct);
            metricsHtml += renderBar('Today Tokens (TPD)', s.daily.tokens, s.daily.tokens_limit, 'TPD', s.daily.tokens_pct);
          }

          if (s.daily.audio_limit > 0) {
            metricsHtml += renderBar('Today Audio Seconds (ASD)', s.daily.audio_seconds, s.daily.audio_limit, 's', s.daily.audio_pct);
          }

          if (s.daily.characters_limit > 0) {
            metricsHtml += renderBar('Minute Characters', s.minute.characters, s.minute.characters_limit, 'chars', (s.minute.characters / s.minute.characters_limit) * 100);
            metricsHtml += renderBar('Today Characters', s.daily.characters, s.daily.characters_limit, 'chars', s.daily.characters_pct);
          }
        }
        metricsHtml += '</div>';

        let headerBox = '';
        if (s.header && (s.header.remaining_requests !== undefined || s.header.remaining_tokens !== undefined)) {
          headerBox = '<div class="usage-headers-box"><strong>Live Headers:</strong>';
          if (s.header.remaining_requests !== undefined) {
            const resetReq = s.header.reset_requests_s ? s.header.reset_requests_s.toFixed(1) + 's' : '--';
            headerBox += `<div>Remaining Requests: ${s.header.remaining_requests} (reset: ${resetReq})</div>`;
          }
          if (s.header.remaining_tokens !== undefined) {
            const resetTok = s.header.reset_tokens_s ? s.header.reset_tokens_s.toFixed(1) + 's' : '--';
            headerBox += `<div>Remaining Tokens: ${Number(s.header.remaining_tokens).toLocaleString()} (reset: ${resetTok})</div>`;
          }
          headerBox += '</div>';
        }

        card.innerHTML = `
          <div class="usage-card-header">
            <div class="usage-service-title">
              <span>${name.toUpperCase()}</span>
            </div>
            <span class="usage-state-pill ${stateClass}">${stateText}</span>
          </div>
          ${pauseHtml}
          ${metricsHtml}
          ${headerBox}
        `;

        el.usageCardsContainer.appendChild(card);
      }
    } catch (e) {
      console.warn("Could not fetch usage status:", e);
    }
  }

  // --- AI Daily Usage History & Pre-Flight Audit ---
  async function fetchUsageHistory(days) {
    if (!el.usageHistoryTbody) return;
    try {
      const token = getToken();
      const numDays = days || (el.usageHistoryRange ? el.usageHistoryRange.value : 7);
      const query = token ? `?days=${encodeURIComponent(numDays)}&token=${encodeURIComponent(token)}` : `?days=${encodeURIComponent(numDays)}`;
      const res = await fetch(`/api/usage/history${query}`);
      if (!res.ok) return;
      const data = await res.json();

      // 1. Update Pre-Flight Banner
      if (data.preflight && el.usagePreflightBanner && el.usagePreflightBadge && el.usagePreflightMsg) {
        const pf = data.preflight;
        el.usagePreflightMsg.textContent = pf.message || '';
        el.usagePreflightBanner.className = 'usage-preflight-banner';
        el.usagePreflightBadge.className = 'usage-status-badge';
        if (pf.health === 'CUTOFF') {
          el.usagePreflightBanner.classList.add('danger');
          el.usagePreflightBadge.classList.add('danger');
          el.usagePreflightBadge.textContent = 'CUTOFF ACTIVE';
        } else if (pf.health === 'NEAR_LIMIT') {
          el.usagePreflightBanner.classList.add('warning');
          el.usagePreflightBadge.classList.add('warning');
          el.usagePreflightBadge.textContent = 'APPROACHING LIMIT';
        } else {
          el.usagePreflightBanner.classList.add('ok');
          el.usagePreflightBadge.classList.add('ok');
          el.usagePreflightBadge.textContent = 'ALL SYSTEMS GO';
        }
      }

      // 2. Render Multi-Day History Table Rows
      el.usageHistoryTbody.innerHTML = '';
      const history = data.history || [];
      if (history.length === 0) {
        el.usageHistoryTbody.innerHTML = '<tr><td colspan="8" style="text-align: center; opacity: 0.6; padding: 1.5rem;">No historical usage recorded in this timeframe.</td></tr>';
        return;
      }

      for (const row of history) {
        const tr = document.createElement('tr');
        const sTagClass = (row.service || '').toLowerCase().includes('edge') ? 'edge_tts' : ((row.service || '').toLowerCase().includes('stt') || (row.service || '').toLowerCase().includes('whisper')) ? 'groq_stt' : '';
        const sName = (row.service || 'UNKNOWN').toUpperCase();
        const p = Math.min(100, Math.max(0, row.max_pct || 0));
        const fillClass = p >= 80 ? 'fill-red' : (p >= 50 ? 'fill-yellow' : 'fill-green');

        let statusBadge = '<span class="usage-status-badge ok">OK</span>';
        if (p >= 80) {
          statusBadge = '<span class="usage-status-badge danger">CUTOFF</span>';
        } else if (p >= 50) {
          statusBadge = '<span class="usage-status-badge warning">HIGH</span>';
        }

        const audioStr = row.audio_seconds > 0 ? `${row.audio_seconds.toFixed(1)}s` : '--';
        const charsStr = row.characters > 0 ? Number(row.characters).toLocaleString() : '--';
        const tokensStr = row.tokens > 0 ? Number(row.tokens).toLocaleString() : '--';

        tr.innerHTML = `
          <td><strong>${row.date}</strong></td>
          <td><span class="service-tag ${sTagClass}">${sName}</span></td>
          <td>${Number(row.requests).toLocaleString()}</td>
          <td>${tokensStr}</td>
          <td>${audioStr}</td>
          <td>${charsStr}</td>
          <td>
            <div class="pct-bar-wrap">
              <div class="pct-bar-track">
                <div class="pct-bar-fill ${fillClass}" style="width: ${p}%"></div>
              </div>
              <span>${p.toFixed(1)}%</span>
            </div>
          </td>
          <td>${statusBadge}</td>
        `;
        el.usageHistoryTbody.appendChild(tr);
      }
    } catch (err) {
      console.warn("Could not fetch usage history:", err);
    }
  }

  // --- Init on DOM Load ---
  window.addEventListener('DOMContentLoaded', () => {
    function safeRun(label, fn) {
      try {
        fn();
      } catch (err) {
        console.error(`[Dashboard] Error in ${label}:`, err);
      }
    }

    safeRun('initTheme', initTheme);
    safeRun('initTabs', initTabs);
    safeRun('initSetupsTab', initSetupsTab);
    safeRun('initTracksTab', initTracksTab);
    safeRun('fetchPersonaSettings', fetchPersonaSettings);
    safeRun('fetchPortSettings', fetchPortSettings);
    safeRun('fetchUsageStatus', fetchUsageStatus);
    safeRun('fetchUsageHistory', fetchUsageHistory);
    if (el.btnRebuildMap) {
      el.btnRebuildMap.addEventListener('click', rebuildTrackMap);
    }
    safeRun('fetchTrackMap', fetchTrackMap);
    safeRun('startWatchdog', startWatchdog);
    safeRun('connectWebSocket', connectWebSocket);
  });
})();

