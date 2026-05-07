const state = {
  config: null,
  slots: [],
  answersByQuestion: [],
  currentQuestionIndex: 0,
  sessionToken: "",
  started: false,
  startChecking: false,
  submitting: false,
};

const runtime = {
  mediaControllers: new Map(),
  playbackToken: 0,
  youtubeApiPromise: null,
  autoplayWarningShown: false,
};

const elements = {
  title: document.getElementById("survey-title"),
  subtitle: document.getElementById("survey-subtitle"),
  assignmentSummary: document.getElementById("assignment-summary"),
  slotCount: document.getElementById("slot-count"),
  randomSourceLabel: document.getElementById("random-source-label"),
  randomSourceHint: document.getElementById("random-source-hint"),
  progressValue: document.getElementById("progress-value"),
  progressNote: document.getElementById("progress-note"),
  viewerLocked: document.getElementById("viewer-locked"),
  viewerContent: document.getElementById("viewer-content"),
  videoGrid: document.getElementById("video-grid"),
  questionCounter: document.getElementById("question-counter"),
  questionText: document.getElementById("question-text"),
  userName: document.getElementById("user-name"),
  accessPasswordField: document.getElementById("access-password-field"),
  accessPassword: document.getElementById("access-password"),
  startSurvey: document.getElementById("start-survey"),
  startReadinessStatus: document.getElementById("start-readiness-status"),
  rerollRandom: document.getElementById("reroll-random"),
  nextQuestion: document.getElementById("next-question"),
  toast: document.getElementById("toast"),
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

function extractYouTubeVideoId(sourceValue) {
  const source = String(sourceValue ?? "").trim();
  if (!source) {
    return "";
  }

  const plainIdPattern = /^[A-Za-z0-9_-]{11}$/;
  if (plainIdPattern.test(source)) {
    return source;
  }

  let parsedUrl;
  try {
    parsedUrl = new URL(source, window.location.href);
  } catch (error) {
    return "";
  }

  const host = parsedUrl.hostname.toLowerCase();
  if (host === "youtu.be" || host === "www.youtu.be") {
    const [candidate = ""] = parsedUrl.pathname.replace(/^\/+/, "").split("/");
    return plainIdPattern.test(candidate) ? candidate : "";
  }

  if (!host.endsWith("youtube.com") && !host.endsWith("youtube-nocookie.com")) {
    return "";
  }

  if (parsedUrl.pathname === "/watch") {
    const candidate = parsedUrl.searchParams.get("v") || "";
    return plainIdPattern.test(candidate) ? candidate : "";
  }

  const segments = parsedUrl.pathname.split("/").filter(Boolean);
  if (segments.length >= 2 && ["embed", "shorts", "live", "v"].includes(segments[0])) {
    return plainIdPattern.test(segments[1]) ? segments[1] : "";
  }

  return "";
}

function getVideoDescriptor(video) {
  if (!video) {
    return { type: "missing", youtubeId: "", url: "" };
  }

  const youtubeId = extractYouTubeVideoId(video.youtubeId || video.url);
  if (youtubeId) {
    return {
      type: "youtube",
      youtubeId,
      url: String(video.url || ""),
    };
  }

  const url = String(video.url || "").trim();
  if (!url) {
    return { type: "missing", youtubeId: "", url: "" };
  }

  return {
    type: "file",
    youtubeId: "",
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

function loadYouTubeIframeApi() {
  if (window.YT && typeof window.YT.Player === "function") {
    return Promise.resolve(window.YT);
  }

  if (runtime.youtubeApiPromise) {
    return runtime.youtubeApiPromise;
  }

  runtime.youtubeApiPromise = new Promise((resolve, reject) => {
    const previousReadyHandler = window.onYouTubeIframeAPIReady;
    window.onYouTubeIframeAPIReady = () => {
      if (typeof previousReadyHandler === "function") {
        previousReadyHandler();
      }
      resolve(window.YT);
    };

    const existingScript = document.querySelector('script[data-role="youtube-iframe-api"]');
    if (existingScript) {
      return;
    }

    const script = document.createElement("script");
    script.src = "https://www.youtube.com/iframe_api";
    script.async = true;
    script.dataset.role = "youtube-iframe-api";
    script.addEventListener("error", () => {
      runtime.youtubeApiPromise = null;
      reject(new Error(bilingual("YouTube API を読み込めませんでした。", "Failed to load the YouTube API.")));
    });
    document.head.appendChild(script);
  });

  return runtime.youtubeApiPromise;
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

function cleanupMediaControllers() {
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
  const controllers = Array.from(runtime.mediaControllers.values());
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
}

function getCurrentQuestion() {
  return state.config.questions[state.currentQuestionIndex];
}

function buildEmptyAnswers() {
  return state.config.questions.map(() => ({}));
}

function resetQuestionFlow() {
  state.currentQuestionIndex = 0;
  state.answersByQuestion = buildEmptyAnswers();
}

function setSlots(resolvedSlots) {
  state.slots = resolvedSlots.map((slot) => ({
    ...slot,
    loading: false,
  }));
}

function getCurrentAnswerMap() {
  return state.answersByQuestion[state.currentQuestionIndex] || {};
}

function getRatingForSlot(slotIndex) {
  const rating = getCurrentAnswerMap()[slotIndex];
  return Number.isInteger(rating) ? rating : null;
}

function setRatingForCurrentQuestion(slotIndex, rating) {
  state.answersByQuestion[state.currentQuestionIndex][slotIndex] = rating;
}

function getAnsweredCountForCurrentQuestion() {
  return state.slots.filter((slot) => Number.isInteger(getRatingForSlot(slot.slotIndex))).length;
}

function isLastQuestion() {
  return state.currentQuestionIndex === state.config.questions.length - 1;
}

function getUserName() {
  return String(elements.userName?.value ?? "").trim();
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

function renderAccessState() {
  const bootstrapped = Boolean(state.config);
  elements.viewerLocked.hidden = state.started;
  elements.viewerContent.hidden = !state.started;
  elements.startSurvey.hidden = state.started;
  if (elements.accessPasswordField) {
    elements.accessPasswordField.hidden = !accessPasswordEnabled();
  }
  elements.startSurvey.textContent = state.startChecking
    ? bilingual("認証と Google Sheets を確認中...", "Checking access and Google Sheets...")
    : bilingual("回答を始める", "Start Survey");
  elements.startSurvey.disabled =
    !bootstrapped || state.started || state.submitting || state.startChecking;
  elements.rerollRandom.disabled = !state.started || state.submitting;
  elements.userName.disabled = state.started || state.startChecking;
  if (elements.accessPassword) {
    elements.accessPassword.disabled = state.started || state.startChecking || !accessPasswordEnabled();
  }
  if (elements.nextQuestion) {
    elements.nextQuestion.disabled = !state.started || state.submitting;
  }
}

function createVideoCard(slot) {
  const card = document.createElement("article");
  card.className = "video-card";
  card.dataset.slotIndex = String(slot.slotIndex);
  const ratingOptions = state.config.scaleLabels
    .map(
      (label, index) => `
        <label class="rating-option" title="${state.config.scaleHints[index]}">
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
      <div class="youtube-player-surface" data-role="youtube-player" hidden></div>
      <video data-role="video" muted loop playsinline preload="auto" hidden></video>
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
  const youtubeSurface = card.querySelector('[data-role="youtube-player"]');
  const fileVideo = card.querySelector('[data-role="video"]');

  placeholder.textContent = message;
  placeholder.hidden = false;
  youtubeSurface.hidden = true;
  fileVideo.hidden = true;

  return {
    ready: Promise.resolve(),
    reset() {},
    play() {
      return Promise.resolve();
    },
    destroy() {},
  };
}

function createFileVideoController(card, descriptor) {
  const placeholder = card.querySelector('[data-role="placeholder"]');
  const fileVideo = card.querySelector('[data-role="video"]');
  const youtubeSurface = card.querySelector('[data-role="youtube-player"]');
  const hidePlaceholder = () => {
    placeholder.hidden = true;
    fileVideo.removeEventListener("loadeddata", hidePlaceholder);
    fileVideo.removeEventListener("canplay", hidePlaceholder);
  };

  youtubeSurface.hidden = true;
  fileVideo.hidden = false;
  placeholder.hidden = false;
  fileVideo.src = descriptor.url;
  fileVideo.addEventListener("loadeddata", hidePlaceholder, { once: true });
  fileVideo.addEventListener("canplay", hidePlaceholder, { once: true });
  fileVideo.load();
  fileVideo.addEventListener(
    "error",
    () => {
      hidePlaceholder();
      fileVideo.hidden = true;
      placeholder.hidden = false;
      placeholder.textContent = bilingual("動画の読み込みに失敗しました。", "Failed to load the video.");
    },
    { once: true },
  );

  return {
    ready: waitForFileVideoReady(fileVideo),
    reset() {
      try {
        fileVideo.currentTime = 0;
      } catch (error) {
        console.debug("Failed to reset local video time", error);
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
      fileVideo.pause();
      fileVideo.removeAttribute("src");
      fileVideo.load();
    },
  };
}

function createYouTubeController(card, descriptor, playbackToken, slotIndex) {
  const placeholder = card.querySelector('[data-role="placeholder"]');
  const fileVideo = card.querySelector('[data-role="video"]');
  const youtubeSurface = card.querySelector('[data-role="youtube-player"]');
  const playerHostId = `youtube-player-${playbackToken}-${slotIndex}`;

  fileVideo.hidden = true;
  youtubeSurface.hidden = false;
  youtubeSurface.id = playerHostId;

  let player = null;
  const ready = new Promise((resolve) => {
    const finish = () => resolve();

    loadYouTubeIframeApi()
      .then((YT) => {
        if (playbackToken !== runtime.playbackToken) {
          finish();
          return;
        }

        player = new YT.Player(playerHostId, {
          width: 512,
          height: 512,
          videoId: descriptor.youtubeId,
          playerVars: {
            autoplay: 0,
            controls: 0,
            disablekb: 1,
            fs: 0,
            iv_load_policy: 3,
            loop: 1,
            origin: window.location.origin,
            playlist: descriptor.youtubeId,
            playsinline: 1,
            rel: 0,
          },
          events: {
            onReady(event) {
              try {
                event.target.mute();
              } catch (error) {
                console.debug("Failed to mute YouTube player", error);
              }
              placeholder.hidden = true;
              finish();
            },
            onStateChange(event) {
              if (event.data === YT.PlayerState.ENDED) {
                try {
                  event.target.seekTo(0, true);
                  event.target.playVideo();
                } catch (error) {
                  console.debug("Failed to loop YouTube player", error);
                }
              }
            },
            onError() {
              youtubeSurface.hidden = true;
              placeholder.hidden = false;
              placeholder.textContent = bilingual("YouTube 動画の読み込みに失敗しました。", "Failed to load the YouTube video.");
              finish();
            },
            onAutoplayBlocked() {
              handleAutoplayBlocked();
            },
          },
        });
      })
      .catch((error) => {
        console.debug("Failed to load YouTube iframe API", error);
        youtubeSurface.hidden = true;
        placeholder.hidden = false;
        placeholder.textContent = bilingual("YouTube API を読み込めませんでした。", "Failed to load the YouTube API.");
        showToast(bilingual("YouTube プレーヤーの読み込みに失敗しました。", "Failed to load the YouTube player."), 4200);
        finish();
      });
  });

  return {
    ready,
    reset() {
      if (!player) {
        return;
      }

      try {
        player.seekTo(0, true);
      } catch (error) {
        console.debug("Failed to reset YouTube player", error);
      }
    },
    play() {
      if (!player) {
        return Promise.resolve();
      }

      try {
        player.mute();
        player.playVideo();
      } catch (error) {
        console.debug("Failed to start YouTube playback", error);
      }
      return Promise.resolve();
    },
    destroy() {
      if (!player) {
        return;
      }

      try {
        player.stopVideo();
      } catch (error) {
        console.debug("Failed to stop YouTube player", error);
      }

      try {
        player.destroy();
      } catch (error) {
        console.debug("Failed to destroy YouTube player", error);
      }
    },
  };
}

function createMediaController(slot, card, playbackToken) {
  const descriptor = getVideoDescriptor(slot.video);
  if (descriptor.type === "missing") {
    return createUnavailableController(card, bilingual("動画情報が見つかりません。", "Video information was not found."));
  }

  if (descriptor.type === "youtube") {
    return createYouTubeController(card, descriptor, playbackToken, slot.slotIndex);
  }

  return createFileVideoController(card, descriptor);
}

function renderVideoGrid() {
  const playbackToken = runtime.playbackToken + 1;
  runtime.playbackToken = playbackToken;
  runtime.autoplayWarningShown = false;
  cleanupMediaControllers();
  elements.videoGrid.innerHTML = "";

  state.slots.forEach((slot) => {
    const card = createVideoCard(slot);
    elements.videoGrid.appendChild(card);
    runtime.mediaControllers.set(slot.slotIndex, createMediaController(slot, card, playbackToken));
  });

  state.slots.forEach(updateVideoCardRating);
  void synchronizeVideoPlayback(playbackToken);
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
  elements.questionCounter.textContent = bilingual(
    `質問 ${state.currentQuestionIndex + 1} / ${state.config.questions.length}`,
    `Question ${state.currentQuestionIndex + 1} / ${state.config.questions.length}`,
  );
  elements.questionText.textContent = question.text;
  elements.nextQuestion.textContent = isLastQuestion()
    ? bilingual("回答を送信", "Submit Responses")
    : bilingual("次の質問", "Next Question");
  state.slots.forEach(updateVideoCardRating);
  updateProgress();
}

function updateProgress() {
  const answeredCount = getAnsweredCountForCurrentQuestion();
  elements.progressValue.textContent = `Q${state.currentQuestionIndex + 1} / ${state.config.questions.length}`;
  elements.progressNote.textContent = bilingual(
    `この設問は ${answeredCount} / ${state.slots.length} 回答済みです`,
    `${answeredCount} / ${state.slots.length} answered for this question`,
  );
  elements.nextQuestion.disabled = !state.started || state.submitting;
  elements.rerollRandom.disabled = !state.started || state.submitting;
}

function validateCurrentQuestion() {
  const missingCount = highlightMissingRatings();
  if (missingCount > 0) {
    showToast(
      bilingual(
        "6本すべての動画に数字を選択してから進んでください。",
        "Select a number for all six videos before proceeding."
      ),
      4200,
    );
    return false;
  }
  return true;
}

function buildSubmissionPayload() {
  const responses = [];

  state.config.questions.forEach((question, questionIndex) => {
    const answers = state.answersByQuestion[questionIndex] || {};
    state.slots.forEach((slot) => {
      const rating = answers[slot.slotIndex];
      if (!Number.isInteger(rating)) {
        throw new Error(bilingual("未回答の設問があります。すべての動画を評価してください。", "Some questions are still unanswered. Rate every video."));
      }
      responses.push({
        questionId: question.id,
        questionIndex,
        questionText: question.text,
        slotIndex: slot.slotIndex,
        slotLabel: slot.slotLabel,
        mode: slot.mode,
        modeLabel: slot.modeLabel,
        rating,
        video: slot.video,
      });
    });
  });

  return {
    sessionToken: state.sessionToken,
    userName: getUserName(),
    responses,
  };
}

async function refreshRandomSlots({ successMessage, errorMessage } = {}) {
  try {
    elements.rerollRandom.disabled = true;
    const response = await fetchJson("/api/resolve-slots", {
      method: "POST",
      body: JSON.stringify({
        refresh: true,
        sessionToken: state.sessionToken,
      }),
    });
    setSlots(response.slots);
    resetQuestionFlow();
    elements.randomSourceLabel.textContent = response.randomSource.label;
    elements.randomSourceHint.textContent = response.randomSource.hint;
    renderVideoGrid();
    renderQuestionState();
    if (successMessage) {
      showToast(successMessage, 4200);
    }
    return true;
  } catch (error) {
    showToast(errorMessage || error.message);
    return false;
  } finally {
    elements.rerollRandom.disabled = false;
  }
}

async function rerollRandomSlots() {
  await refreshRandomSlots({
    successMessage: bilingual(
      "動画2〜6を再抽選しました。評価は最初の質問からやり直してください。",
      "Videos 2 to 6 were reshuffled. Please restart ratings from the first question."
    ),
    errorMessage: bilingual("動画2〜6の再抽選に失敗しました。", "Failed to reshuffle Videos 2 to 6."),
  });
}

async function submitSurvey() {
  if (!validateUserName()) {
    return;
  }

  state.submitting = true;
  elements.rerollRandom.disabled = true;
  elements.nextQuestion.textContent = bilingual("送信中...", "Submitting...");
  updateProgress();

  try {
    const payload = buildSubmissionPayload();
    const response = await fetchJson("/api/submissions", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    downloadSubmissionCsv(response.submissionCsv, response.downloadFilename);
    const submissionStatusMessage = buildSubmissionStatusMessage(response);
    await refreshRandomSlots({
      successMessage: `${submissionStatusMessage} ${bilingual("次の動画セットへ更新しました。", "The next video set has been loaded.")}`,
      errorMessage: `${submissionStatusMessage} ${bilingual("ただし次の動画セットへの更新に失敗しました。", "However, loading the next video set failed.")}`,
    });
  } catch (error) {
    showToast(error.message);
  } finally {
    state.submitting = false;
    elements.rerollRandom.disabled = false;
    renderQuestionState();
  }
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
  renderAccessState();

  try {
    const response = await fetchJson("/api/start-session", {
      method: "POST",
      body: JSON.stringify({
        userName: getUserName(),
        startPassword: getAccessPassword(),
      }),
    });
    state.sessionToken = String(response.sessionToken || "");
    setSlots(response.slots || []);
    resetQuestionFlow();
    elements.randomSourceLabel.textContent = response.randomSource?.label || "";
    elements.randomSourceHint.textContent = response.randomSource?.hint || "";
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
    renderAccessState();
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

  state.started = true;
  renderAccessState();
  renderVideoGrid();
  renderQuestionState();
  showToast(
    bilingual(
      "Google Sheets の保存確認後に回答を開始しました。",
      "The survey started after Google Sheets availability was confirmed."
    ),
    2800,
  );
}

async function handleNextQuestion() {
  if (state.submitting) {
    return;
  }

  if (!validateCurrentQuestion()) {
    return;
  }

  if (isLastQuestion()) {
    await submitSurvey();
    return;
  }

  state.currentQuestionIndex += 1;
  renderQuestionState();
}

async function bootstrap() {
  try {
    const payload = await fetchJson("/api/bootstrap");
    state.config = payload;
    setSlots(payload.slotsResolved || []);
    resetQuestionFlow();

    elements.title.textContent = payload.title;
    elements.subtitle.textContent = payload.subtitle;
    elements.assignmentSummary.textContent = payload.assignmentSummary;
    elements.slotCount.textContent = String(payload.slots);
    elements.randomSourceLabel.textContent = payload.randomSource.label;
    elements.randomSourceHint.textContent = payload.randomSource.hint;
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
    renderAccessState();
  } catch (error) {
    showToast(error.message, 6000);
  }
}

elements.startSurvey.addEventListener("click", handleStartSurvey);
elements.rerollRandom.addEventListener("click", rerollRandomSlots);
elements.nextQuestion.addEventListener("click", handleNextQuestion);
elements.userName?.addEventListener("input", clearUserNameInvalidState);
elements.accessPassword?.addEventListener("input", clearAccessPasswordInvalidState);

bootstrap();
