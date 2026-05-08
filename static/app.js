const state = {
  config: null,
  slots: [],
  shapeRounds: [],
  answersByQuestion: [],
  currentQuestionIndex: 0,
  currentShapeIndex: 0,
  sessionToken: "",
  phase: "start",
  startChecking: false,
  introLoading: false,
  introReady: false,
  advancing: false,
  submitting: false,
};

const runtime = {
  mediaControllers: new Map(),
  referenceController: null,
  playbackToken: 0,
  autoplayWarningShown: false,
  syncLoopId: 0,
  preloadedFileVideos: new Map(),
  preparedShapeRounds: new Map(),
};

const elements = {
  startStage: document.getElementById("start-stage"),
  questionIntroStage: document.getElementById("question-intro-stage"),
  surveyStage: document.getElementById("survey-stage"),
  completionStage: document.getElementById("completion-stage"),
  progressValue: document.getElementById("progress-value"),
  progressNote: document.getElementById("progress-note"),
  progressFill: document.getElementById("progress-fill"),
  videoGrid: document.getElementById("video-grid"),
  laneScroll: document.getElementById("lane-scroll"),
  referencePanel: document.getElementById("reference-panel"),
  questionCounter: document.getElementById("question-counter"),
  questionText: document.getElementById("question-text"),
  shapePrompt: document.getElementById("shape-prompt"),
  scaleLegend: document.getElementById("scale-legend"),
  userName: document.getElementById("user-name"),
  accessPasswordField: document.getElementById("access-password-field"),
  accessPassword: document.getElementById("access-password"),
  startSurvey: document.getElementById("start-survey"),
  startReadinessStatus: document.getElementById("start-readiness-status"),
  introQuestionText: document.getElementById("intro-question-text"),
  beginQuestion: document.getElementById("begin-question"),
  adminNextIntro: document.getElementById("admin-next-intro"),
  nextQuestion: document.getElementById("next-question"),
  adminNextSurvey: document.getElementById("admin-next-survey"),
  completionMessage: document.getElementById("completion-message"),
  toast: document.getElementById("toast"),
  preloadBin: document.getElementById("preload-bin"),
};

function bilingual(japanese, english) {
  const ja = String(japanese ?? "").trim();
  const en = String(english ?? "").trim();
  if (!ja) {
    return en;
  }
  if (!en) {
    return ja;
  }
  return `${ja}「${en}」`;
}

function showToast(message, timeout = 3600) {
  elements.toast.textContent = message;
  elements.toast.hidden = false;
  window.clearTimeout(showToast.timerId);
  showToast.timerId = window.setTimeout(() => {
    elements.toast.hidden = true;
  }, timeout);
}

function wait(milliseconds) {
  return new Promise((resolve) => {
    window.setTimeout(resolve, milliseconds);
  });
}

const PROMPT_TRANSLATIONS = {
  "A dinosaur lowering its head": "頭を下げる恐竜",
  "A character raising both hands": "両手を上げるキャラクター",
  "A character throwing their hands up in the air": "両手を勢いよく空へ突き上げるキャラクター",
  "A dinosaur shakes its head from side to side, then raises it and roars": "左右に頭を振ったあと、頭を持ち上げて咆哮する恐竜",
  "A bear rearing up on its hind legs": "後ろ足で立ち上がるクマ",
  "A character moving both arms backward": "両腕を後ろへ動かすキャラクター",
  "A character putting their hands behind their back": "手を背中の後ろに回すキャラクター",
  "A character raising a sword high": "剣を高く掲げるキャラクター",
  "A character holding a sword aloft": "剣を高々と掲げるキャラクター",
  "A character spreads their long limbs, appearing larger": "長い手足を広げて大きく見せるキャラクター",
};

function fitTextToSingleLine(element, options = {}) {
  if (!element || element.hidden) {
    return;
  }

  const minSize = Number(options.minSize ?? 16);
  const maxSize = Number(options.maxSize ?? 40);
  const precision = Number(options.precision ?? 0.5);
  const availableWidth = element.clientWidth;
  if (!availableWidth) {
    return;
  }

  let low = minSize;
  let high = maxSize;
  let best = minSize;
  element.style.fontSize = `${maxSize}px`;

  while ((high - low) > precision) {
    const mid = (low + high) / 2;
    element.style.fontSize = `${mid}px`;
    if (element.scrollWidth <= availableWidth) {
      best = mid;
      low = mid;
    } else {
      high = mid;
    }
  }

  element.style.fontSize = `${best}px`;
}

function fitQuestionTextBlocks() {
  fitTextToSingleLine(elements.introQuestionText, { minSize: 12, maxSize: 42 });
  fitTextToSingleLine(elements.questionText, { minSize: 12, maxSize: 40 });
  fitTextToSingleLine(elements.shapePrompt, { minSize: 12, maxSize: 40 });
}

function splitPromptVariants(promptText) {
  const seen = new Set();
  return String(promptText ?? "")
    .split("/")
    .map((value) => value.trim())
    .filter((value) => {
      if (!value || seen.has(value)) {
        return false;
      }
      seen.add(value);
      return true;
    });
}

function formatShapePrompt(promptText) {
  const englishVariants = splitPromptVariants(promptText);
  if (!englishVariants.length) {
    return "";
  }

  const japaneseVariants = englishVariants
    .map((variant) => PROMPT_TRANSLATIONS[variant] || "")
    .filter(Boolean);

  const englishText = englishVariants.join(" / ");
  if (!japaneseVariants.length) {
    return englishText;
  }

  const japaneseText = Array.from(new Set(japaneseVariants)).join(" / ");
  return japaneseText === englishText ? japaneseText : bilingual(japaneseText, englishText);
}

function enableWheelHorizontalScroll() {
  const scroller = elements.laneScroll;
  if (!scroller || scroller.dataset.wheelBound === "true") {
    return;
  }

  scroller.dataset.wheelBound = "true";
  scroller.addEventListener(
    "wheel",
    (event) => {
      if (Math.abs(event.deltaY) <= Math.abs(event.deltaX)) {
        return;
      }

      if (scroller.scrollWidth <= scroller.clientWidth) {
        return;
      }

      event.preventDefault();
      scroller.scrollLeft += event.deltaY;
    },
    { passive: false },
  );
}

function resetLaneScrollPosition() {
  const scroller = elements.laneScroll;
  if (!scroller) {
    return;
  }

  scroller.scrollLeft = 0;
}

function getVideoDescriptor(video) {
  if (!video) {
    return { type: "missing", url: "" };
  }

  const url = String(video.url || "").trim();
  if (!url) {
    return { type: "missing", url: "" };
  }

  return {
    type: "file",
    url: new URL(url, window.location.href).href,
  };
}

function handleAutoplayBlocked() {
  if (runtime.autoplayWarningShown) {
    return;
  }

  runtime.autoplayWarningShown = true;
  showToast(
    bilingual(
      "ブラウザ側で自動再生が制限されました。ページを一度クリックしてから再読み込みしてください。",
      "Autoplay was blocked by the browser. Click once on the page and reload."
    ),
    5200,
  );
}

async function fetchJson(url, options = {}) {
  const response = await fetch(url, {
    headers: {
      "Content-Type": "application/json",
      ...(options.headers ?? {}),
    },
    ...options,
  });
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(payload.error || bilingual("通信に失敗しました。", "The request failed."));
  }
  return payload;
}

function downloadSubmissionCsv(csvText, filename) {
  if (!csvText || !filename) {
    return;
  }

  const blob = new Blob(["\uFEFF", csvText], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function buildSubmissionStatusMessage(response) {
  const parts = [
    `${response.message} ${bilingual(`${response.file} に ${response.rowsWritten} 行追加しました。`, `${response.rowsWritten} rows were appended to ${response.file}.`)}`,
  ];

  if (response.downloadFilename) {
    parts.push(bilingual(`端末へ ${response.downloadFilename} を保存しました。`, `${response.downloadFilename} was saved to this device.`));
  }

  if (response.mailMessage) {
    parts.push(response.mailMessage);
  }

  if (response.appsScriptMessage) {
    parts.push(response.appsScriptMessage);
  }

  return parts.join(" ");
}

function waitForFileVideoReady(video) {
  if (video.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA) {
    return Promise.resolve();
  }

  return new Promise((resolve) => {
    const finish = () => {
      video.removeEventListener("loadeddata", finish);
      video.removeEventListener("canplay", finish);
      video.removeEventListener("error", finish);
      resolve();
    };

    video.addEventListener("loadeddata", finish, { once: true });
    video.addEventListener("canplay", finish, { once: true });
    video.addEventListener("error", finish, { once: true });
  });
}

function getVideoCacheKey(video) {
  if (!video) {
    return "";
  }
  return String(video.id || video.videoCode || video.objectKey || video.url || "").trim();
}

function moveVideoToPreloadBin(video) {
  if (!video || !elements.preloadBin) {
    return;
  }
  video.hidden = true;
  elements.preloadBin.appendChild(video);
}

function createPreloadVideoElement(slot) {
  const descriptor = getVideoDescriptor(slot.video);
  if (descriptor.type !== "file") {
    return null;
  }

  const video = document.createElement("video");
  video.muted = true;
  video.loop = true;
  video.playsInline = true;
  video.preload = "auto";
  video.disablePictureInPicture = true;
  video.hidden = true;
  video.src = descriptor.url;
  moveVideoToPreloadBin(video);
  video.load();
  return video;
}

function ensureShapeRoundPrepared(shapeRound) {
  if (!shapeRound) {
    return Promise.resolve();
  }

  const cacheKey = String(shapeRound.shapeIndex);
  if (runtime.preparedShapeRounds.has(cacheKey)) {
    return runtime.preparedShapeRounds.get(cacheKey);
  }

  const promise = Promise.all(
    shapeRound.slots.map((slot) => {
      const videoKey = getVideoCacheKey(slot.video);
      const descriptor = getVideoDescriptor(slot.video);
      if (descriptor.type !== "file" || !videoKey) {
        return Promise.resolve();
      }

      let video = runtime.preloadedFileVideos.get(videoKey);
      if (!video) {
        video = createPreloadVideoElement(slot);
        if (!video) {
          return Promise.resolve();
        }
        runtime.preloadedFileVideos.set(videoKey, video);
      }
      return waitForFileVideoReady(video);
    }),
  )
    .then(() => shapeRound)
    .catch((error) => {
      runtime.preparedShapeRounds.delete(cacheKey);
      throw error;
    });

  runtime.preparedShapeRounds.set(cacheKey, promise);
  return promise;
}

function getUpcomingShapeRound() {
  return state.shapeRounds[state.currentShapeIndex + 1] || null;
}

function warmUpcomingShapeRound() {
  const upcomingShapeRound = getUpcomingShapeRound();
  if (!upcomingShapeRound) {
    return;
  }
  void ensureShapeRoundPrepared(upcomingShapeRound).catch((error) => {
    console.debug("Failed to warm the upcoming shape round", error);
  });
}

function clearPlaybackSyncLoop() {
  if (runtime.syncLoopId) {
    window.clearInterval(runtime.syncLoopId);
    runtime.syncLoopId = 0;
  }
}

function primePlaybackControllers() {
  getPlaybackControllers().forEach((controller) => {
    Promise.resolve(controller.play()).catch((error) => {
      console.debug("Immediate playback prime failed", error);
    });
  });
}

function destroyReferenceController() {
  if (!runtime.referenceController) {
    return;
  }

  try {
    runtime.referenceController.destroy();
  } catch (error) {
    console.debug("Failed to destroy reference controller", error);
  }
  runtime.referenceController = null;
}

function getPlaybackControllers() {
  const controllers = Array.from(runtime.mediaControllers.values());
  if (runtime.referenceController) {
    controllers.push(runtime.referenceController);
  }
  return controllers;
}

function cleanupMediaControllers() {
  clearPlaybackSyncLoop();
  destroyReferenceController();
  runtime.mediaControllers.forEach((controller) => {
    try {
      controller.destroy();
    } catch (error) {
      console.debug("Failed to destroy media controller", error);
    }
  });
  runtime.mediaControllers.clear();
}

async function synchronizeVideoPlayback(playbackToken) {
  const controllers = getPlaybackControllers();
  if (!controllers.length) {
    return;
  }

  await Promise.all(
    controllers.map((controller) => Promise.resolve(controller.ready).catch(() => undefined)),
  );

  if (playbackToken !== runtime.playbackToken) {
    return;
  }

  controllers.forEach((controller) => {
    try {
      controller.reset();
    } catch (error) {
      console.debug("Failed to reset media", error);
    }
  });

  await wait(100);
  if (playbackToken !== runtime.playbackToken) {
    return;
  }

  await Promise.all(
    controllers.map((controller) =>
      Promise.resolve(controller.play()).catch((error) => {
        console.debug("Playback start failed", error);
      }),
    ),
  );

  clearPlaybackSyncLoop();
  runtime.syncLoopId = window.setInterval(() => {
    if (playbackToken !== runtime.playbackToken) {
      clearPlaybackSyncLoop();
      return;
    }

    const activeControllers = getPlaybackControllers().filter(
      (controller) =>
        typeof controller.getCurrentTime === "function" &&
        typeof controller.seekTo === "function" &&
        controller.canResync !== false,
    );
    if (activeControllers.length <= 1) {
      return;
    }

    const masterTime = activeControllers[0].getCurrentTime();
    if (!Number.isFinite(masterTime)) {
      return;
    }

    activeControllers.slice(1).forEach((controller) => {
      const currentTime = controller.getCurrentTime();
      if (!Number.isFinite(currentTime)) {
        return;
      }
      if (Math.abs(currentTime - masterTime) > 0.06) {
        controller.seekTo(masterTime);
      }
    });
  }, 250);
}

function getCurrentQuestion() {
  return state.config?.questions?.[state.currentQuestionIndex] || null;
}

function isSimilarityQuestion(question = getCurrentQuestion()) {
  return question?.id === "similarity_to_video_0" || question?.id === "similarity_to_video_1";
}

function isTextAlignmentQuestion(question = getCurrentQuestion()) {
  return question?.id === "text_alignment";
}

function getCurrentShapePrompt() {
  const currentShapeRound = getCurrentShapeRound();
  if (!currentShapeRound) {
    return "";
  }

  const videos = [
    currentShapeRound.referenceVideo,
    ...(currentShapeRound.slots || []).map((slot) => slot.video),
  ];
  for (const video of videos) {
    const promptText = String(video?.promptText || "").trim();
    if (promptText) {
      return promptText;
    }
  }
  return "";
}

function getScaleHintsForQuestion(question = getCurrentQuestion()) {
  const questionId = question?.id || "";
  if (questionId === "naturalness") {
    return [
      bilingual("とても不自然", "Very unnatural"),
      bilingual("やや不自然", "Somewhat unnatural"),
      bilingual("どちらでもない", "Neutral"),
      bilingual("やや自然", "Somewhat natural"),
      bilingual("とても自然", "Very natural"),
    ];
  }
  if (questionId === "similarity_to_video_0" || questionId === "similarity_to_video_1") {
    return [
      bilingual("まったく近くない", "Very different"),
      bilingual("あまり近くない", "Somewhat different"),
      bilingual("どちらでもない", "Neutral"),
      bilingual("やや近い", "Somewhat similar"),
      bilingual("とても近い", "Very similar"),
    ];
  }
  if (questionId === "shape_consistency") {
    return [
      bilingual("とても不一致", "Very inconsistent"),
      bilingual("やや不一致", "Somewhat inconsistent"),
      bilingual("どちらでもない", "Neutral"),
      bilingual("やや一貫している", "Somewhat consistent"),
      bilingual("とても一貫している", "Very consistent"),
    ];
  }
  if (questionId === "text_alignment") {
    return [
      bilingual("まったく整合していない", "Very misaligned"),
      bilingual("あまり整合していない", "Somewhat misaligned"),
      bilingual("どちらでもない", "Neutral"),
      bilingual("やや整合している", "Somewhat aligned"),
      bilingual("とても整合している", "Very aligned"),
    ];
  }
  return state.config?.scaleHints || [
    bilingual("低い", "Low"),
    bilingual("やや低い", "Somewhat low"),
    bilingual("普通", "Neutral"),
    bilingual("やや高い", "Somewhat high"),
    bilingual("高い", "High"),
  ];
}

function buildEmptyAnswers() {
  const questionCount = state.config?.questions?.length || 0;
  const shapeCount = state.shapeRounds.length;
  return Array.from({ length: questionCount }, () =>
    Array.from({ length: shapeCount }, () => ({})),
  );
}

function resetQuestionFlow() {
  state.currentQuestionIndex = 0;
  state.currentShapeIndex = 0;
  state.answersByQuestion = buildEmptyAnswers();
  state.introLoading = false;
  state.introReady = false;
  state.advancing = false;
  syncSlotsFromCurrentShape();
}

function setShapeRounds(shapeRounds) {
  runtime.preparedShapeRounds.clear();
  runtime.preloadedFileVideos.forEach((video) => {
    try {
      video.pause();
      video.removeAttribute("src");
      video.load();
      video.remove();
    } catch (error) {
      console.debug("Failed to reset preloaded video", error);
    }
  });
  runtime.preloadedFileVideos.clear();
  state.shapeRounds = shapeRounds.map((shapeRound) => ({
    ...shapeRound,
    slots: (shapeRound.slots || []).map((slot) => ({ ...slot, loading: false })),
  }));
  syncSlotsFromCurrentShape();
}

function syncSlotsFromCurrentShape() {
  const currentShapeRound = getCurrentShapeRound();
  state.slots = currentShapeRound
    ? currentShapeRound.slots.map((slot) => ({ ...slot, loading: false }))
    : [];
  if (elements.videoGrid) {
    elements.videoGrid.style.setProperty(
      "--lane-slot-count",
      String(Math.max(state.slots.length, 1)),
    );
  }
}

function getCurrentShapeRound() {
  return state.shapeRounds[state.currentShapeIndex] || null;
}

function getCurrentAnswerMap() {
  return state.answersByQuestion[state.currentQuestionIndex]?.[state.currentShapeIndex] || {};
}

function getRatingForSlot(slotIndex) {
  const rating = getCurrentAnswerMap()[slotIndex];
  return Number.isInteger(rating) ? rating : null;
}

function setRatingForCurrentQuestion(slotIndex, rating) {
  if (!state.answersByQuestion[state.currentQuestionIndex]) {
    state.answersByQuestion[state.currentQuestionIndex] = [];
  }
  if (!state.answersByQuestion[state.currentQuestionIndex][state.currentShapeIndex]) {
    state.answersByQuestion[state.currentQuestionIndex][state.currentShapeIndex] = {};
  }
  state.answersByQuestion[state.currentQuestionIndex][state.currentShapeIndex][slotIndex] = rating;
}

function getAnsweredCountForCurrentQuestion() {
  return state.slots.filter((slot) => Number.isInteger(getRatingForSlot(slot.slotIndex))).length;
}

function isLastQuestion() {
  return state.currentQuestionIndex === (state.config?.questions?.length || 1) - 1;
}

function isLastShapeForQuestion() {
  return state.currentShapeIndex === state.shapeRounds.length - 1;
}

function isLastSurveyStep() {
  return isLastQuestion() && isLastShapeForQuestion();
}

function getUserName() {
  return String(elements.userName?.value ?? "").trim();
}

function isAdminUser() {
  return getUserName().toLowerCase() === "admin";
}

function accessPasswordEnabled() {
  return Boolean(state.config?.accessControl?.enabled);
}

function getAccessPassword() {
  return String(elements.accessPassword?.value ?? "");
}

function clearUserNameInvalidState() {
  elements.userName?.classList.remove("is-invalid");
}

function clearAccessPasswordInvalidState() {
  elements.accessPassword?.classList.remove("is-invalid");
}

function setStartReadinessStatus(message = "", variant = "neutral") {
  if (!elements.startReadinessStatus) {
    return;
  }

  const text = String(message ?? "").trim();
  elements.startReadinessStatus.hidden = text.length === 0;
  elements.startReadinessStatus.textContent = text;
  elements.startReadinessStatus.dataset.state = variant;
}

function validateUserName() {
  const userName = getUserName();
  const isValid = userName.length > 0;
  elements.userName?.classList.toggle("is-invalid", !isValid);
  if (!isValid) {
    elements.userName?.focus();
    showToast(bilingual("User名を入力してから送信してください。", "Enter a user name before continuing."), 4200);
  }
  return isValid;
}

function validateAccessPassword() {
  if (!accessPasswordEnabled()) {
    return true;
  }

  const password = getAccessPassword();
  const isValid = password.length > 0;
  elements.accessPassword?.classList.toggle("is-invalid", !isValid);
  if (!isValid) {
    elements.accessPassword?.focus();
    showToast(
      bilingual(
        "開始パスワードを入力してから開始してください。",
        "Enter the start password before starting."
      ),
      4200,
    );
  }
  return isValid;
}

function renderAppPhase() {
  const bootstrapped = Boolean(state.config);
  const isStartPhase = state.phase === "start";
  const isIntroPhase = state.phase === "questionIntro";
  const isSurveyPhase = state.phase === "survey";
  const isCompletedPhase = state.phase === "completed";
  const showAdminAdvance = isAdminUser();

  document.body.dataset.phase = state.phase;
  elements.startStage.hidden = !isStartPhase;
  elements.questionIntroStage.hidden = !isIntroPhase;
  elements.surveyStage.hidden = !isSurveyPhase;
  elements.completionStage.hidden = !isCompletedPhase;

  if (elements.accessPasswordField) {
    elements.accessPasswordField.hidden = !accessPasswordEnabled();
  }
  elements.startSurvey.textContent = state.startChecking
    ? bilingual("認証と Google Sheets を確認中...", "Checking access and Google Sheets...")
    : bilingual("回答を始める", "Start Survey");
  elements.startSurvey.disabled =
    !bootstrapped || !isStartPhase || state.submitting || state.startChecking;
  elements.userName.disabled = !isStartPhase || state.startChecking;
  if (elements.accessPassword) {
    elements.accessPassword.disabled = !isStartPhase || state.startChecking || !accessPasswordEnabled();
  }
  if (elements.beginQuestion) {
    elements.beginQuestion.disabled =
      !isIntroPhase || state.introLoading || !state.introReady || state.advancing;
  }
  if (elements.adminNextIntro) {
    elements.adminNextIntro.hidden = !(isIntroPhase && showAdminAdvance);
    elements.adminNextIntro.disabled =
      !isIntroPhase || state.introLoading || !state.introReady || state.advancing;
  }
  if (elements.nextQuestion) {
    elements.nextQuestion.disabled = !isSurveyPhase || state.submitting || state.advancing;
  }
  if (elements.adminNextSurvey) {
    elements.adminNextSurvey.hidden = !(isSurveyPhase && showAdminAdvance);
    elements.adminNextSurvey.disabled = !isSurveyPhase || state.submitting || state.advancing;
  }

  window.requestAnimationFrame(fitQuestionTextBlocks);
}

function renderQuestionIntroState() {
  const question = getCurrentQuestion();
  if (elements.introQuestionText) {
    elements.introQuestionText.textContent = question?.text || bilingual("読み込み中...", "Loading...");
  }

  if (!elements.beginQuestion) {
    return;
  }

  if (state.introLoading) {
    elements.beginQuestion.textContent = bilingual("動画を準備中...", "Preparing videos...");
  } else {
    elements.beginQuestion.textContent = bilingual("回答を始める", "Begin Rating");
  }

  fitQuestionTextBlocks();
}

function createVideoCard(slot) {
  const card = document.createElement("article");
  card.className = "video-card";
  card.dataset.slotIndex = String(slot.slotIndex);
  const scaleHints = getScaleHintsForQuestion();
  const ratingOptions = state.config.scaleLabels
    .map(
      (label, index) => `
        <label class="rating-option" data-rating-index="${index}" title="${scaleHints[index] || ""}">
          <input
            type="radio"
            name="rating-${slot.slotIndex}"
            value="${index + 1}"
            aria-label="${slot.slotLabel} を ${label} で評価"
          />
          <span class="rating-chip">
            <strong>${label}</strong>
          </span>
        </label>
      `,
    )
    .join("");

  card.innerHTML = `
    <div class="slot-topline">
      <div>
        <h3 class="slot-title">${slot.slotLabel}</h3>
      </div>
    </div>
    <div class="video-frame">
      <div class="video-placeholder" data-role="placeholder">${bilingual("動画を読み込んでいます...", "Loading video...")}</div>
      <video data-role="video" muted loop playsinline preload="auto" disablepictureinpicture tabindex="-1" hidden></video>
      <div class="video-interaction-shield" aria-hidden="true"></div>
    </div>
    <div class="video-rating">
      <div class="rating-grid">${ratingOptions}</div>
    </div>
  `;

  card.querySelectorAll(`input[name="rating-${slot.slotIndex}"]`).forEach((input) => {
    input.addEventListener("change", (event) => {
      setRatingForCurrentQuestion(slot.slotIndex, Number(event.target.value));
      clearMissingState(slot.slotIndex);
      updateProgress();
    });
  });

  return card;
}

function createUnavailableController(card, message) {
  const placeholder = card.querySelector('[data-role="placeholder"]');
  const fileVideo = card.querySelector('[data-role="video"]');

  placeholder.textContent = message;
  placeholder.hidden = false;
  fileVideo.hidden = true;

  return {
    ready: Promise.resolve(),
    canResync: false,
    reset() {},
    getCurrentTime() {
      return 0;
    },
    seekTo() {},
    play() {
      return Promise.resolve();
    },
    destroy() {},
  };
}

function createFileVideoController(card, descriptor) {
  const placeholder = card.querySelector('[data-role="placeholder"]');
  const fileVideo = card.querySelector('[data-role="video"]');
  const hidePlaceholder = () => {
    placeholder.hidden = true;
    fileVideo.removeEventListener("loadeddata", hidePlaceholder);
    fileVideo.removeEventListener("canplay", hidePlaceholder);
  };
  const handleError = () => {
    hidePlaceholder();
    fileVideo.hidden = true;
    placeholder.hidden = false;
    placeholder.textContent = bilingual("動画の読み込みに失敗しました。", "Failed to load the video.");
  };

  fileVideo.hidden = false;
  fileVideo.muted = true;
  fileVideo.loop = true;
  fileVideo.playsInline = true;
  fileVideo.preload = "auto";
  fileVideo.disablePictureInPicture = true;
  placeholder.hidden = fileVideo.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA;

  if (!fileVideo.currentSrc || fileVideo.currentSrc !== descriptor.url) {
    placeholder.hidden = false;
    fileVideo.src = descriptor.url;
    fileVideo.load();
  }

  fileVideo.addEventListener("loadeddata", hidePlaceholder, { once: true });
  fileVideo.addEventListener("canplay", hidePlaceholder, { once: true });
  fileVideo.addEventListener("error", handleError, { once: true });

  return {
    ready: waitForFileVideoReady(fileVideo),
    canResync: true,
    reset() {
      try {
        fileVideo.currentTime = 0;
      } catch (error) {
        console.debug("Failed to reset local video time", error);
      }
    },
    getCurrentTime() {
      return Number.isFinite(fileVideo.currentTime) ? fileVideo.currentTime : 0;
    },
    seekTo(seconds) {
      try {
        fileVideo.currentTime = Number.isFinite(seconds) ? Math.max(seconds, 0) : 0;
      } catch (error) {
        console.debug("Failed to seek local video", error);
      }
    },
    async play() {
      try {
        await fileVideo.play();
      } catch (error) {
        handleAutoplayBlocked();
      }
    },
    destroy() {
      fileVideo.removeEventListener("error", handleError);
      fileVideo.pause();
      try {
        fileVideo.currentTime = 0;
      } catch (error) {
        console.debug("Failed to reset local video during destroy", error);
      }
      fileVideo.removeAttribute("src");
      fileVideo.load();
    },
  };
}

function createMediaController(slot, card, playbackToken) {
  const descriptor = getVideoDescriptor(slot.video);
  if (descriptor.type === "missing") {
    return createUnavailableController(card, bilingual("動画情報が見つかりません。", "Video information was not found."));
  }

  return createFileVideoController(card, descriptor);
}

function createReferenceCard(slot) {
  const card = document.createElement("article");
  card.className = "reference-card";
  card.innerHTML = `
    <div class="slot-topline">
      <div>
        <h3 class="slot-title">${slot.slotLabel}</h3>
      </div>
    </div>
    <p class="reference-caption">${bilingual(
      "比較基準として固定表示しています。",
      "Shown as a fixed reference for comparison.",
    )}</p>
    <div class="video-frame">
      <div class="video-placeholder" data-role="placeholder">${bilingual("動画を読み込んでいます...", "Loading video...")}</div>
      <video data-role="video" muted loop playsinline preload="auto" disablepictureinpicture tabindex="-1" hidden></video>
      <div class="video-interaction-shield" aria-hidden="true"></div>
    </div>
  `;
  return card;
}

async function renderReferencePanel() {
  if (!elements.referencePanel) {
    return;
  }

  destroyReferenceController();
  elements.referencePanel.hidden = true;
  elements.referencePanel.innerHTML = "";

  if (!isSimilarityQuestion() || !state.slots.length) {
    return;
  }

  const currentShapeRound = getCurrentShapeRound();
  if (!currentShapeRound?.referenceVideo) {
    return;
  }

  const referenceSlot = {
    slotIndex: currentShapeRound.referenceSlotIndex ?? 0,
    slotLabel: currentShapeRound.referenceSlotLabel || state.config.referenceSlotLabel || bilingual("動画0", "Video 0"),
    video: currentShapeRound.referenceVideo,
  };

  const card = createReferenceCard(referenceSlot);
  elements.referencePanel.appendChild(card);
  elements.referencePanel.hidden = false;

  const controller = createMediaController(referenceSlot, card, runtime.playbackToken, {
    reusePreloaded: false,
  });
  runtime.referenceController = controller;

  const sourceController = runtime.mediaControllers.get(referenceSlot.slotIndex);
  try {
    await Promise.all([
      Promise.resolve(sourceController?.ready).catch(() => undefined),
      Promise.resolve(controller.ready).catch(() => undefined),
    ]);
    const currentTime = typeof sourceController?.getCurrentTime === "function"
      ? sourceController.getCurrentTime()
      : 0;
    if (typeof controller.seekTo === "function") {
      controller.seekTo(currentTime);
    }
    await Promise.resolve(controller.play()).catch((error) => {
      console.debug("Reference playback start failed", error);
    });
  } catch (error) {
    console.debug("Failed to prepare reference panel", error);
  }
}

function renderVideoGrid() {
  const playbackToken = runtime.playbackToken + 1;
  runtime.playbackToken = playbackToken;
  runtime.autoplayWarningShown = false;
  cleanupMediaControllers();
  resetLaneScrollPosition();
  elements.videoGrid.innerHTML = "";

  state.slots.forEach((slot) => {
    const card = createVideoCard(slot);
    elements.videoGrid.appendChild(card);
    runtime.mediaControllers.set(slot.slotIndex, createMediaController(slot, card, playbackToken));
  });

  state.slots.forEach(updateVideoCardRating);
  void synchronizeVideoPlayback(playbackToken);
  warmUpcomingShapeRound();
}

function updateVideoCardRating(slot) {
  const rating = getRatingForSlot(slot.slotIndex);
  const card = elements.videoGrid.querySelector(`[data-slot-index="${slot.slotIndex}"]`);
  if (!card) {
    return;
  }

  card.querySelectorAll(`input[name="rating-${slot.slotIndex}"]`).forEach((input) => {
    input.checked = Number(input.value) === rating;
  });
}

function clearMissingState(slotIndex) {
  const card = elements.videoGrid.querySelector(`[data-slot-index="${slotIndex}"]`);
  if (card) {
    card.classList.remove("is-missing");
  }
}

function updateScaleHintsForVisibleCards(question = getCurrentQuestion()) {
  const scaleHints = getScaleHintsForQuestion(question);
  elements.videoGrid
    ?.querySelectorAll(".rating-option")
    .forEach((option) => {
      const ratingIndex = Number(option.dataset.ratingIndex || "0");
      option.title = scaleHints[ratingIndex] || "";
    });
}

function highlightMissingRatings() {
  let missingCount = 0;
  state.slots.forEach((slot) => {
    const card = elements.videoGrid.querySelector(`[data-slot-index="${slot.slotIndex}"]`);
    const isMissing = !Number.isInteger(getRatingForSlot(slot.slotIndex));
    if (card) {
      card.classList.toggle("is-missing", isMissing);
    }
    if (isMissing) {
      missingCount += 1;
    }
  });
  return missingCount;
}

function renderQuestionState() {
  const question = getCurrentQuestion();
  const currentShapeRound = getCurrentShapeRound();
  if (!question || !currentShapeRound) {
    return;
  }
  elements.questionCounter.textContent = bilingual(
    `質問 ${state.currentQuestionIndex + 1} / ${state.config.questions.length}`,
    `Question ${state.currentQuestionIndex + 1} / ${state.config.questions.length}`,
  );
  elements.questionText.textContent = question.text;
  if (elements.shapePrompt) {
    const shapePrompt = getCurrentShapePrompt();
    const shouldShowShapePrompt = isTextAlignmentQuestion(question) && Boolean(shapePrompt);
    elements.shapePrompt.hidden = !shouldShowShapePrompt;
    elements.shapePrompt.textContent = shouldShowShapePrompt
      ? `テキスト「Text」: ${formatShapePrompt(shapePrompt)}`
      : "";
  }
  if (elements.scaleLegend) {
    const scaleHints = getScaleHintsForQuestion(question);
    const negativeLabel = scaleHints[0] || bilingual("低い", "Low");
    const positiveLabel =
      scaleHints[scaleHints.length - 1] ||
      bilingual("高い", "High");
    elements.scaleLegend.textContent = `1 = ${negativeLabel} / 5 = ${positiveLabel}`;
  }
  if (state.advancing) {
    elements.nextQuestion.textContent = bilingual("読み込み中...", "Loading...");
    state.slots.forEach(updateVideoCardRating);
    updateScaleHintsForVisibleCards(question);
    updateProgress();
    void renderReferencePanel();
    return;
  }
  if (isLastSurveyStep()) {
    elements.nextQuestion.textContent = bilingual("回答を送信", "Submit Responses");
  } else if (isLastShapeForQuestion()) {
    elements.nextQuestion.textContent = bilingual("次の質問へ", "Next Question");
  } else {
    elements.nextQuestion.textContent = bilingual("次の形状へ", "Next Shape");
  }
  state.slots.forEach(updateVideoCardRating);
  updateScaleHintsForVisibleCards(question);
  updateProgress();
  void renderReferencePanel();
  fitQuestionTextBlocks();
}

function updateProgress() {
  const answeredCount = getAnsweredCountForCurrentQuestion();
  const shapeCount = state.shapeRounds.length || 1;
  const totalSteps = (state.config?.questions?.length || 1) * shapeCount;
  const currentStep = (state.currentQuestionIndex * shapeCount) + state.currentShapeIndex + 1;
  elements.progressValue.textContent = `Q${state.currentQuestionIndex + 1} / ${state.config.questions.length} · S${state.currentShapeIndex + 1} / ${shapeCount}`;
  elements.progressNote.textContent = bilingual(
    `形状 ${state.currentShapeIndex + 1} / ${shapeCount} ・ ${answeredCount} / ${state.slots.length} 回答済み`,
    `Shape ${state.currentShapeIndex + 1} / ${shapeCount} · ${answeredCount} / ${state.slots.length} answered`,
  );
  const progressRatio = totalSteps
    ? currentStep / totalSteps
    : 0;
  if (elements.progressFill) {
    elements.progressFill.style.width = `${Math.max(0, Math.min(progressRatio, 1)) * 100}%`;
  }
  elements.nextQuestion.disabled = state.phase !== "survey" || state.submitting || state.advancing;
}

function validateCurrentQuestion() {
  const missingCount = highlightMissingRatings();
  if (missingCount > 0) {
    showToast(
      bilingual(
        `この形状の ${state.slots.length} 本すべてに数字を選択してから進んでください。`,
        `Select a number for all ${state.slots.length} videos in this shape before proceeding.`
      ),
      4200,
    );
    return false;
  }
  return true;
}

function fillMissingRatingsForCurrentShape(defaultRating = 3) {
  state.slots.forEach((slot) => {
    if (!Number.isInteger(getRatingForSlot(slot.slotIndex))) {
      setRatingForCurrentQuestion(slot.slotIndex, defaultRating);
    }
  });
}

function buildSubmissionPayload() {
  const responses = [];

  state.config.questions.forEach((question, questionIndex) => {
    const answerSets = state.answersByQuestion[questionIndex] || [];
    state.shapeRounds.forEach((shapeRound, shapeIndex) => {
      const answers = answerSets[shapeIndex] || {};
      shapeRound.slots.forEach((slot) => {
        const rating = answers[slot.slotIndex];
        if (!Number.isInteger(rating)) {
          throw new Error(bilingual("未回答の設問があります。すべての動画を評価してください。", "Some questions are still unanswered. Rate every video."));
        }
        responses.push({
          questionId: question.id,
          questionIndex,
          questionText: question.text,
          shapeIndex,
          shapeId: shapeRound.shapeId,
          shapeLabel: shapeRound.shapeLabel,
          slotIndex: slot.slotIndex,
          slotLabel: slot.slotLabel,
          mode: slot.mode,
          modeLabel: slot.modeLabel,
          rating,
          video: slot.video,
        });
      });
    });
  });

  return {
    sessionToken: state.sessionToken,
    userName: getUserName(),
    responses,
  };
}

async function submitSurvey() {
  if (!validateUserName()) {
    return;
  }

  state.submitting = true;
  elements.nextQuestion.textContent = bilingual("送信中...", "Submitting...");
  updateProgress();

  try {
    const payload = buildSubmissionPayload();
    const response = await fetchJson("/api/submissions", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    downloadSubmissionCsv(response.submissionCsv, response.downloadFilename);
    elements.toast.hidden = true;
    cleanupMediaControllers();
    state.phase = "completed";
    state.sessionToken = "";
    elements.completionMessage.textContent = bilingual(
      "お疲れ様でした。回答はローカルパスに保存されています。",
      "Thank you. Your responses have been saved to the local path."
    );
    renderAppPhase();
  } catch (error) {
    showToast(error.message);
  } finally {
    state.submitting = false;
    if (state.phase === "survey") {
      renderQuestionState();
    }
  }
}

async function enterQuestionIntro() {
  cleanupMediaControllers();
  resetLaneScrollPosition();
  if (elements.videoGrid) {
    elements.videoGrid.innerHTML = "";
  }
  if (elements.referencePanel) {
    elements.referencePanel.innerHTML = "";
    elements.referencePanel.hidden = true;
  }
  state.introLoading = true;
  state.introReady = false;
  state.phase = "questionIntro";
  renderQuestionIntroState();
  renderAppPhase();

  try {
    await ensureShapeRoundPrepared(getCurrentShapeRound());
    state.introReady = true;
  } catch (error) {
    console.debug("Failed to prepare the current shape round", error);
    showToast(
      bilingual(
        "次の動画セットの準備に失敗しました。もう一度お試しください。",
        "Failed to prepare the next video set. Please try again."
      ),
      4800,
    );
  } finally {
    state.introLoading = false;
    renderQuestionIntroState();
    renderAppPhase();
  }
}

async function beginCurrentQuestion() {
  if (state.introLoading || !state.introReady || !getCurrentShapeRound()) {
    return;
  }

  state.phase = "survey";
  renderAppPhase();
  renderVideoGrid();
  renderQuestionState();
  primePlaybackControllers();
}

async function runStartReadinessCheck() {
  state.sessionToken = "";
  state.startChecking = true;
  setStartReadinessStatus(
    bilingual(
      "開始パスワードと Google Sheets を確認しています。",
      "Checking the start password and Google Sheets availability."
    ),
    "pending",
  );
  renderAppPhase();

  try {
    const response = await fetchJson("/api/start-session", {
      method: "POST",
      body: JSON.stringify({
        userName: getUserName(),
        startPassword: getAccessPassword(),
      }),
    });
    state.sessionToken = String(response.sessionToken || "");
    setShapeRounds(response.shapeRounds || []);
    resetQuestionFlow();
    const message =
      response.message ||
      bilingual(
        "Google Sheets への保存確認が完了しました。",
        "Google Sheets readiness check completed."
      );
    setStartReadinessStatus(message, response.status === "disabled" ? "info" : "success");
    return true;
  } catch (error) {
    const message =
      error.message ||
      bilingual(
        "開始パスワードまたは Google Sheets の確認に失敗しました。",
        "Start password or Google Sheets verification failed."
      );
    if (accessPasswordEnabled()) {
      elements.accessPassword?.classList.add("is-invalid");
    }
    setStartReadinessStatus(message, "error");
    showToast(message, 4800);
    return false;
  } finally {
    state.startChecking = false;
    renderAppPhase();
  }
}

async function handleStartSurvey() {
  if (!state.config) {
    showToast(bilingual("初期化中です。少し待ってから開始してください。", "Initializing. Please wait a moment before starting."), 3200);
    return;
  }

  if (!validateUserName()) {
    return;
  }

  if (!validateAccessPassword()) {
    return;
  }

  const isReadyToStart = await runStartReadinessCheck();
  if (!isReadyToStart) {
    return;
  }

  await enterQuestionIntro();
}

async function advanceSurveyPage({ allowIncomplete = false } = {}) {
  if (state.submitting || state.advancing) {
    return;
  }

  if (!allowIncomplete && !validateCurrentQuestion()) {
    return;
  }
  if (allowIncomplete) {
    fillMissingRatingsForCurrentShape(3);
  }

  if (isLastSurveyStep()) {
    await submitSurvey();
    return;
  }

  state.advancing = true;
  renderQuestionState();

  try {
    if (isLastShapeForQuestion()) {
      state.currentQuestionIndex += 1;
      state.currentShapeIndex = 0;
      syncSlotsFromCurrentShape();
      await enterQuestionIntro();
      return;
    }

    state.currentShapeIndex += 1;
    syncSlotsFromCurrentShape();
    renderVideoGrid();
    renderQuestionState();
    primePlaybackControllers();
  } finally {
    state.advancing = false;
    if (state.phase === "survey") {
      renderQuestionState();
    } else {
      renderAppPhase();
    }
  }
}

async function handleNextQuestion() {
  await advanceSurveyPage({ allowIncomplete: false });
}

async function handleAdminNextSurvey() {
  if (!isAdminUser()) {
    return;
  }
  await advanceSurveyPage({ allowIncomplete: true });
}

async function bootstrap() {
  try {
    const payload = await fetchJson("/api/bootstrap");
    state.config = payload;
    setShapeRounds(payload.shapeRounds || []);
    resetQuestionFlow();
    setStartReadinessStatus(
      bilingual(
        accessPasswordEnabled()
          ? "開始時にパスワード認証と Google Sheets の保存状態を自動確認します。"
          : "開始時に Google Sheets の保存状態を自動確認します。",
        accessPasswordEnabled()
          ? "Password authentication and Google Sheets availability will be checked automatically before starting."
          : "Google Sheets availability will be checked automatically before starting."
      ),
      "info",
    );
    if (elements.completionMessage) {
      elements.completionMessage.textContent = bilingual(
        "お疲れ様でした。回答はローカルパスに保存されています。",
        "Thank you. Your responses have been saved to the local path."
      );
    }
    renderQuestionIntroState();
    renderAppPhase();
  } catch (error) {
    showToast(error.message, 6000);
  }
}

elements.startSurvey.addEventListener("click", handleStartSurvey);
elements.beginQuestion?.addEventListener("click", beginCurrentQuestion);
elements.adminNextIntro?.addEventListener("click", beginCurrentQuestion);
elements.nextQuestion.addEventListener("click", handleNextQuestion);
elements.adminNextSurvey?.addEventListener("click", handleAdminNextSurvey);
elements.userName?.addEventListener("input", clearUserNameInvalidState);
elements.userName?.addEventListener("input", renderAppPhase);
elements.accessPassword?.addEventListener("input", clearAccessPasswordInvalidState);
window.addEventListener("resize", fitQuestionTextBlocks);

bootstrap();
enableWheelHorizontalScroll();
