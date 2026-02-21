(function() {
  const path = window.location.pathname;
  const isGame = path.startsWith('/elpres/room/');
  if (!isGame) return;

  const params = new URLSearchParams(window.location.search);
  const playerIdFromUrl = params.get('id') || '';
  const playerNameFromUrl = params.get('name') || '';
  const roomMatch = path.match(/\/elpres\/room\/([^/]+)/);
  const roomName = roomMatch ? decodeURIComponent(roomMatch[1]) : '';

  if (!roomName || !playerIdFromUrl.trim()) {
    window.location.href = '/elpres/';
    return;
  }

  const wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  const wsHost = window.location.host;
  let wsUrl = `${wsProtocol}//${wsHost}/elpres/ws?room=${encodeURIComponent(roomName)}&id=${encodeURIComponent(playerIdFromUrl.trim())}`;
  if (playerNameFromUrl.trim()) {
    wsUrl += '&name=' + encodeURIComponent(playerNameFromUrl.trim());
  }

  let state = null;
  let playerId = null;
  let pendingPlay = [];
  let ws = null;
  let dontReconnect = false;
  let heartbeatIntervalId = null;
  const HEARTBEAT_INTERVAL_MS = 5000;

  const container = document.getElementById('game-container');
  const pileContainer = document.getElementById('card-pile-container');
  const pileCircle = document.getElementById('pile-circle');
  const pileCrown = document.getElementById('pile-crown');
  const pileEl = document.getElementById('card-pile');
  const pileDropZone = document.getElementById('pile-drop-zone');
  const handEl = document.getElementById('player-hand');
  const handContainer = document.getElementById('player-hand-container');
  const passBtn = document.getElementById('pass-btn');
  const playersEl = document.getElementById('players-status');
  const myAccoladeEl = document.getElementById('my-accolade-icon');
  const scoreOverlay = document.getElementById('score-overlay');
  const scoreCountdown = document.getElementById('score-countdown');
  const scoreList = document.getElementById('score-list');
  const lobbyOverlay = document.getElementById('lobby-overlay');
  const lobbyPlayerList = document.getElementById('lobby-player-list');
  const startGameBtn = document.getElementById('start-game-btn');
  const alertOverlay = document.getElementById('alert-overlay');
  const alertMessage = document.getElementById('alert-message');
  const alertOk = document.getElementById('alert-ok');
  const leaveConfirmOverlay = document.getElementById('leave-confirm-overlay');
  const leaveCancelBtn = document.getElementById('leave-cancel-btn');
  const leaveConfirmBtn = document.getElementById('leave-confirm-btn');
  const restartConfirmOverlay = document.getElementById('restart-confirm-overlay');
  const restartCancelBtn = document.getElementById('restart-cancel-btn');
  const restartConfirmBtn = document.getElementById('restart-confirm-btn');
  const logoutBtn = document.getElementById('logout-btn');
  const restartBtn = document.getElementById('restart-btn');
  const spectatorCountLabel = document.getElementById('spectator-count-label');
  const rankLabelsToggleBtn = document.getElementById('rank-labels-toggle-btn');
  const cardsFaceToggleBtn = document.getElementById('cards-face-toggle-btn');
  const passPositionToggleBtn = document.getElementById('pass-position-toggle-btn');
  const spectatorToggleBtn = document.getElementById('spectator-toggle-btn');
  const waitingDisconnectedOverlay = document.getElementById('waiting-disconnected-overlay');
  const waitingDisconnectedMessage = document.getElementById('waiting-disconnected-message');
  const waitingDisconnectedCountdown = document.getElementById('waiting-disconnected-countdown');
  const autoPassOverlay = document.getElementById('auto-pass-overlay');
  const autoPassMessage = document.getElementById('auto-pass-message');
  const autoPassCountdown = document.getElementById('auto-pass-countdown');

  let waitingDisconnectedIntervalId = null;
  let waitingDisconnectedSeconds = 0;
  let openingPlayTimerId = null;
  let turnTimerPhase1Id = null;
  let turnTimerPhase2Id = null;
  let turnCountdownIntervalId = null;
  let lastWasMyTurn = false;
  const TURN_WARN_AFTER_MS = 30000;
  const TURN_AUTO_PASS_AFTER_MS = 30000;
  const INACTIVITY_NOSE_MS = 30000;
  const OPENING_PLAY_TIMEOUT_MS = 10000;
  let inactiveNoseTimeoutId = null;
  let showInactiveNoseForPlayerIdx = null;
  const CARDS_FACE_DOWN_KEY = 'elpres_cards_face_down';
  const CARDS_RANK_LABELS_KEY = 'elpres_cards_rank_labels';
  const PASS_POSITION_ABOVE_KEY = 'elpres_pass_position_above';
  let cardsFaceDown = false;
  let showRankLabels = false;
  let passPositionAbove = false;
  try {
    cardsFaceDown = localStorage.getItem(CARDS_FACE_DOWN_KEY) === 'true';
    showRankLabels = localStorage.getItem(CARDS_RANK_LABELS_KEY) === 'true';
    passPositionAbove = localStorage.getItem(PASS_POSITION_ABOVE_KEY) === 'true';
  } catch (_) {}
  if (handContainer) handContainer.classList.toggle('pass-above-cards', passPositionAbove);

  function showAlert(message) {
    alertMessage.textContent = message;
    alertOverlay.classList.remove('hidden');
  }

  function hideAlert() {
    alertOverlay.classList.add('hidden');
  }

  alertOk.addEventListener('click', () => {
    hideAlert();
    if (alertMessage.dataset.redirect) {
      window.location.href = '/elpres/';
    }
  });

  if (logoutBtn) {
    logoutBtn.addEventListener('click', () => {
      if (state && state.phase === 'no_game') {
        dontReconnect = true;
        window.location.href = '/elpres/';
        return;
      }
      leaveConfirmOverlay.classList.remove('hidden');
    });
  }
  if (leaveCancelBtn) {
    leaveCancelBtn.addEventListener('click', () => {
      leaveConfirmOverlay.classList.add('hidden');
    });
  }
  if (leaveConfirmBtn) {
    leaveConfirmBtn.addEventListener('click', () => {
      leaveConfirmOverlay.classList.add('hidden');
      dontReconnect = true;
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'leave' }));
      }
    });
  }
  if (restartBtn) {
    restartBtn.addEventListener('click', () => {
      restartConfirmOverlay.classList.remove('hidden');
    });
  }
  if (restartCancelBtn) {
    restartCancelBtn.addEventListener('click', () => {
      restartConfirmOverlay.classList.add('hidden');
    });
  }
  const restartVoteOverlay = document.getElementById('restart-vote-overlay');
  const restartVoteMessage = document.getElementById('restart-vote-message');
  const restartVoteNopeBtn = document.getElementById('restart-vote-nope-btn');
  const restartVoteYesBtn = document.getElementById('restart-vote-yes-btn');
  const restartRejectedOverlay = document.getElementById('restart-rejected-overlay');

  if (restartConfirmBtn) {
    restartConfirmBtn.addEventListener('click', () => {
      restartConfirmOverlay.classList.add('hidden');
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'request_restart_vote' }));
      }
    });
  }
  if (restartVoteYesBtn) {
    restartVoteYesBtn.addEventListener('click', () => {
      restartVoteOverlay.classList.add('hidden');
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'restart_vote', vote: 'yes' }));
      }
    });
  }
  if (restartVoteNopeBtn) {
    restartVoteNopeBtn.addEventListener('click', () => {
      restartVoteOverlay.classList.add('hidden');
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'restart_vote', vote: 'no' }));
      }
    });
  }
  if (rankLabelsToggleBtn) {
    rankLabelsToggleBtn.addEventListener('click', () => {
      showRankLabels = !showRankLabels;
      try { localStorage.setItem(CARDS_RANK_LABELS_KEY, String(showRankLabels)); } catch (_) {}
      rankLabelsToggleBtn.classList.toggle('active', showRankLabels);
      if (state) render();
    });
  }
  if (cardsFaceToggleBtn) {
    cardsFaceToggleBtn.addEventListener('click', () => {
      cardsFaceDown = !cardsFaceDown;
      try { localStorage.setItem(CARDS_FACE_DOWN_KEY, String(cardsFaceDown)); } catch (_) {}
      cardsFaceToggleBtn.classList.toggle('active', cardsFaceDown);
      if (state) render();
    });
  }
  if (passPositionToggleBtn) {
    passPositionToggleBtn.addEventListener('click', () => {
      passPositionAbove = !passPositionAbove;
      try { localStorage.setItem(PASS_POSITION_ABOVE_KEY, String(passPositionAbove)); } catch (_) {}
      passPositionToggleBtn.classList.toggle('active', passPositionAbove);
      handContainer.classList.toggle('pass-above-cards', passPositionAbove);
    });
  }

  function cardDisplay(card) {
    const r = card.rank === 'T' ? '10' : card.rank;
    return `${r}${card.suit}`;
  }

  /** Card filename for SVG (e.g. 3C, 10H, QS) without extension. */
  function cardFilename(card) {
    const r = card.rank === 'T' ? '10' : (card.rank || '');
    return r + (card.suit || '');
  }

  function cardFrontImg(card) {
    const img = document.createElement('img');
    img.src = '/elpres/cards/' + cardFilename(card) + '.svg';
    img.alt = cardDisplay(card);
    img.className = 'card-front-img';
    return img;
  }

  /** Display rank for overlay: 2..10, J, Q, K, A (server uses T for 10). */
  function cardRankDisplay(card) {
    const r = card.rank === 'T' ? '10' : (card.rank || '');
    return r;
  }

  /** True for diamonds and hearts (red), false for clubs and spades (black). */
  function cardSuitIsRed(card) {
    const s = (card.suit || '').toUpperCase();
    return s === 'D' || s === 'H';
  }

  /** Face-up card content: img only, or img + rank overlay when showRankLabels is on. */
  function cardFrontContent(card) {
    const img = cardFrontImg(card);
    if (!showRankLabels) return img;
    const wrap = document.createElement('div');
    wrap.className = 'card-front-wrapper';
    wrap.appendChild(img);
    const overlay = document.createElement('div');
    overlay.className = 'card-rank-overlay ' + (cardSuitIsRed(card) ? 'suit-red' : 'suit-black');
    const rankDisplay = cardRankDisplay(card);
    const topLeft = document.createElement('span');
    topLeft.className = 'card-rank-top-left';
    topLeft.textContent = rankDisplay;
    const bottomRight = document.createElement('span');
    bottomRight.className = 'card-rank-bottom-right';
    bottomRight.textContent = rankDisplay;
    overlay.appendChild(topLeft);
    overlay.appendChild(bottomRight);
    wrap.appendChild(overlay);
    return wrap;
  }

  function cardBackImg() {
    const img = document.createElement('img');
    img.src = '/elpres/cards/back.svg';
    img.alt = '';
    img.className = 'card-back-img';
    return img;
  }

  /** Normalized key for matching server valid_plays (rank 10 as "T"). */
  function cardKey(c) {
    if (!c) return '';
    const r = c.rank === '10' ? 'T' : (c.rank || '');
    return r + ':' + (c.suit || '');
  }

  function connect() {
    if (heartbeatIntervalId) {
      clearInterval(heartbeatIntervalId);
      heartbeatIntervalId = null;
    }
    ws = new WebSocket(wsUrl);
    ws.onopen = () => {
      heartbeatIntervalId = setInterval(() => {
        if (ws && ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({ type: 'heartbeat' }));
        }
      }, HEARTBEAT_INTERVAL_MS);
    };
    ws.onmessage = (e) => {
      try {
        const msg = JSON.parse(e.data);
        if (msg.type === 'state') {
          state = msg.state;
          playerId = msg.player_id;
          if (inactiveNoseTimeoutId) {
            clearTimeout(inactiveNoseTimeoutId);
            inactiveNoseTimeoutId = null;
          }
          showInactiveNoseForPlayerIdx = null;
          const results = state?.results || [];
          if (results.length) {
            results.forEach((pid) => {
              const p = (state?.players || []).find(x => String(x.id) === String(pid));
              if (p) p.hand = [];
            });
          }
          const pilePlays = state?.round?.pile?.plays || [];
          const myIdx = (state?.players || []).findIndex(p => p.id === playerId);
          const isOurTurn = state?.current_player_idx === myIdx && state?.phase === 'Playing';
          if (pilePlays.length === 0 && !isOurTurn) {
            pendingPlay.length = 0;
          }
          if (state?.phase === 'Playing' && typeof state.current_player_idx === 'number' && state.current_player_idx >= 0) {
            const idx = state.current_player_idx;
            inactiveNoseTimeoutId = setTimeout(() => {
              inactiveNoseTimeoutId = null;
              showInactiveNoseForPlayerIdx = idx;
              render();
            }, INACTIVITY_NOSE_MS);
          }
          render();
        } else if (msg.type === 'player_disconnected') {
          if (ws && ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({ type: 'state_request' }));
          }
        } else if (msg.type === 'player_joined') {
          if (state) {
            state.players = state.players || [];
            const p = msg.player;
            if (p && !state.players.some(x => x.id === p.id)) {
              state.players.push({ id: p.id, name: p.name });
            }
          }
          render();
        } else if (msg.type === 'game_over') {
          applyRoundResults(msg.results || []);
          render();
          setTimeout(() => showScoreScreen(msg.results || []), 3000);
        } else if (msg.type === 'error') {
          console.error(msg.message);
          if (msg.message === 'Unknown player; join from lobby first' && playerNameFromUrl.trim()) {
            dontReconnect = true;
            const name = playerNameFromUrl.trim();
            fetch('/elpres/join?room=' + encodeURIComponent(roomName) + '&name=' + encodeURIComponent(name))
              .then(function(r) { return r.json(); })
              .then(function(data) {
                if (data.error) {
                  showAlert(msg.message);
                  alertMessage.dataset.redirect = '1';
                  return;
                }
                if (data.id) {
                  window.location.href = window.location.pathname + '?id=' + encodeURIComponent(data.id) + '&name=' + encodeURIComponent(name);
                  return;
                }
                showAlert(msg.message);
                alertMessage.dataset.redirect = '1';
              })
              .catch(function() {
                showAlert(msg.message);
                alertMessage.dataset.redirect = '1';
              });
            return;
          }
          if (msg.message !== 'Not your turn') {
            dontReconnect = true;
            alertMessage.dataset.redirect = '1';
          }
          showAlert(msg.message);
        } else if (msg.type === 'you_left') {
          dontReconnect = true;
          window.location.href = '/elpres/';
        } else if (msg.type === 'restart_vote_requested') {
          const name = msg.initiator_name || 'Someone';
          if (restartVoteMessage) restartVoteMessage.textContent = name + ' wants to restart the game';
          if (restartVoteOverlay) restartVoteOverlay.classList.remove('hidden');
          if (restartConfirmOverlay) restartConfirmOverlay.classList.add('hidden');
        } else if (msg.type === 'restart_vote_rejected') {
          if (restartVoteOverlay) restartVoteOverlay.classList.add('hidden');
          if (restartRejectedOverlay) {
            restartRejectedOverlay.classList.remove('hidden');
            setTimeout(() => restartRejectedOverlay.classList.add('hidden'), 5000);
          }
        } else if (msg.type === 'restart_vote_passed') {
          if (restartVoteOverlay) restartVoteOverlay.classList.add('hidden');
        }
      } catch (err) {
        console.error('Parse error', err);
      }
    };
    ws.onclose = () => {
      if (heartbeatIntervalId) {
        clearInterval(heartbeatIntervalId);
        heartbeatIntervalId = null;
      }
      if (!dontReconnect) setTimeout(connect, 2000);
    };
  }

  function applyRoundResults(results) {
    if (!state || !state.players || !results.length) return;
    const n = results.length;
    results.forEach((pid, i) => {
      const p = state.players.find(x => String(x.id) === String(pid));
      if (p) {
        p.result_position = i + 1;
        p.hand = [];
        if (i === 0) p.accolade = 'ElPresidente';
        else if (i === n - 1) p.accolade = 'Shithead';
        else if (i === 1) p.accolade = 'VP';
        else p.accolade = 'Pleb';
      }
    });
  }

  function showScoreScreen(results) {
    const g = state?.phase !== 'no_game' ? state : null;
    if (!g) return;
    const players = g.players || [];
    const ordered = [];
    for (const pid of results) {
      const p = players.find(x => x.id === pid);
      if (p) ordered.push(p);
    }
    scoreList.innerHTML = ordered.map((p, i) => {
      const pos = i + 1;
      let acc = '';
      if (pos === 1) acc = ' 👑';
      if (pos === players.length) acc = ' 💩';
      return `<li>${p.name}${acc}</li>`;
    }).join('');
    scoreOverlay.classList.remove('hidden');
    const SCORE_DISPLAY_SECONDS = 10;
    let remaining = SCORE_DISPLAY_SECONDS;
    scoreCountdown.textContent = String(remaining);
    const intervalId = setInterval(() => {
      remaining--;
      scoreCountdown.textContent = String(remaining);
      if (remaining <= 0) {
        clearInterval(intervalId);
      }
    }, 1000);
    setTimeout(() => {
      clearInterval(intervalId);
      scoreOverlay.classList.add('hidden');
    }, SCORE_DISPLAY_SECONDS * 1000);
  }

  function render() {
    if (!state) return;
    if (state.phase === 'no_game') {
      clearOpeningPlayTimer();
      clearTurnPassTimer();
      lastWasMyTurn = false;
      if (inactiveNoseTimeoutId) {
        clearTimeout(inactiveNoseTimeoutId);
        inactiveNoseTimeoutId = null;
      }
      showInactiveNoseForPlayerIdx = null;
      myAccoladeEl.textContent = '';
      myAccoladeEl.classList.add('hidden');
    }

    if (state.phase === 'no_game') {
      document.body.classList.remove('spectator-view');
      spectatorToggleBtn.style.display = 'none';
      spectatorToggleBtn.classList.add('hidden');
      if (restartBtn) restartBtn.style.display = 'none';
      const spectatorCountText = document.querySelector('#spectator-count-label .spectator-count-text');
      if (spectatorCountText) spectatorCountText.textContent = '';
      if (rankLabelsToggleBtn) rankLabelsToggleBtn.style.display = 'none';
      if (cardsFaceToggleBtn) cardsFaceToggleBtn.style.display = 'none';
      if (passPositionToggleBtn) passPositionToggleBtn.style.display = 'none';
      lobbyOverlay.classList.remove('hidden');
      const raw = state.players || [];
      const seen = new Set();
      const players = raw.filter(p => {
        if (seen.has(p.id)) return false;
        seen.add(p.id);
        return true;
      });
      startGameBtn.disabled = players.length < 2;
      lobbyPlayerList.innerHTML = players.map(p =>
        `<li class="${p.id === playerId ? 'me' : ''}">${escapeHtml(p.name)}</li>`
      ).join('');
      container.classList.add('hidden');
      return;
    }

    lobbyOverlay.classList.add('hidden');
    container.classList.remove('hidden');
    document.body.classList.toggle('spectator-view', state.spectator === true);
    if (restartBtn) restartBtn.style.display = state.spectator === true ? 'none' : '';
    if (spectatorCountLabel) {
      const textEl = spectatorCountLabel.querySelector('.spectator-count-text');
      const n = typeof state.spectator_count === 'number' ? state.spectator_count : 0;
      if (textEl) textEl.textContent = n > 0 ? String(n) : '';
    }
    if (rankLabelsToggleBtn) {
      rankLabelsToggleBtn.style.display = state.spectator === true ? 'none' : '';
      rankLabelsToggleBtn.classList.toggle('active', showRankLabels);
    }
    if (cardsFaceToggleBtn) {
      cardsFaceToggleBtn.style.display = state.spectator === true ? 'none' : '';
      cardsFaceToggleBtn.classList.toggle('active', cardsFaceDown);
    }
    if (passPositionToggleBtn) {
      passPositionToggleBtn.style.display = state.spectator === true ? 'none' : '';
      passPositionToggleBtn.classList.toggle('active', passPositionAbove);
    }

    const g = state;
    const players = g.players || [];
    const myIdx = players.findIndex(p => String(p.id) === String(playerId));
    const isMyTurn = g.current_player_idx === myIdx && g.phase === 'Playing';
    const validPlays = g.valid_plays || [];
    const pileEmpty = (g.round?.pile?.plays || []).length === 0;
    if (!pileEmpty || !isMyTurn) clearOpeningPlayTimer();

    const w = state.waiting_for_disconnected;
    const isWaitingForMe = w && g.current_player_idx === myIdx;
    if (w && w.player_name != null && !isWaitingForMe) {
      waitingDisconnectedMessage.textContent = 'Waiting for ' + escapeHtml(w.player_name) + '…';
      waitingDisconnectedSeconds = typeof w.seconds_remaining === 'number' ? w.seconds_remaining : 60;
      waitingDisconnectedCountdown.textContent = String(waitingDisconnectedSeconds);
      waitingDisconnectedOverlay.classList.remove('hidden');
      if (!waitingDisconnectedIntervalId) {
        waitingDisconnectedIntervalId = setInterval(() => {
          waitingDisconnectedSeconds = Math.max(0, waitingDisconnectedSeconds - 1);
          waitingDisconnectedCountdown.textContent = String(waitingDisconnectedSeconds);
          if (waitingDisconnectedSeconds <= 0 && waitingDisconnectedIntervalId) {
            clearInterval(waitingDisconnectedIntervalId);
            waitingDisconnectedIntervalId = null;
          }
        }, 1000);
      }
    } else {
      waitingDisconnectedOverlay.classList.add('hidden');
      if (waitingDisconnectedIntervalId) {
        clearInterval(waitingDisconnectedIntervalId);
        waitingDisconnectedIntervalId = null;
      }
    }

    if (g.phase === 'Playing') {
      if (!isMyTurn) {
        clearTurnPassTimer();
      } else if (state.spectator !== true && !lastWasMyTurn) {
        startTurnPassTimer();
      }
    }

    if (g.phase === 'Trading' && g.trading) {
      clearOpeningPlayTimer();
      clearTurnPassTimer();
      lastWasMyTurn = false;
      if (inactiveNoseTimeoutId) {
        clearTimeout(inactiveNoseTimeoutId);
        inactiveNoseTimeoutId = null;
      }
      showInactiveNoseForPlayerIdx = null;
      pileCircle.classList.remove('pile-circle-my-turn');
      container.classList.remove('game-container-my-turn');
      renderTradePile(g, myIdx);
      renderHand(g, myIdx, false, []);
      passBtn.style.display = 'none';
      renderPlayersStatus(g, myIdx);
      const iAmDickTagged = state?.dick_tagged_player_id != null && String(state.dick_tagged_player_id) === String(playerId);
      myAccoladeEl.textContent = iAmDickTagged ? '🍆' : '';
      myAccoladeEl.classList.toggle('hidden', !iAmDickTagged);
      if (state.spectator === true) {
        spectatorToggleBtn.style.display = '';
        spectatorToggleBtn.classList.remove('hidden');
        const wantsToPlay = state.wants_to_play !== false;
        spectatorToggleBtn.textContent = wantsToPlay ? 'You will join next game' : "You won't join next game";
        spectatorToggleBtn.classList.toggle('subdued', !wantsToPlay);
      } else {
        spectatorToggleBtn.style.display = 'none';
        spectatorToggleBtn.classList.add('hidden');
        spectatorToggleBtn.classList.remove('subdued');
      }
      return;
    }

    renderPile(g, isMyTurn);
    pileCircle.classList.toggle('pile-circle-my-turn', isMyTurn);
    container.classList.toggle('game-container-my-turn', isMyTurn);
    if (state.spectator === true) {
      spectatorToggleBtn.style.display = '';
      spectatorToggleBtn.classList.remove('hidden');
      const wantsToPlay = state.wants_to_play !== false;
      spectatorToggleBtn.textContent = wantsToPlay ? 'You will join next game' : "You won't join next game";
      spectatorToggleBtn.classList.toggle('subdued', !wantsToPlay);
      passBtn.style.display = 'none';
    } else {
      spectatorToggleBtn.style.display = 'none';
      spectatorToggleBtn.classList.add('hidden');
      spectatorToggleBtn.classList.remove('subdued');
    }
    renderHand(g, myIdx, isMyTurn, validPlays);
    renderPassButton(g, myIdx, isMyTurn);
    renderPlayersStatus(g, myIdx);
    const me = myIdx >= 0 ? g.players[myIdx] : null;
    const hasFinished = me && (me.hand || []).length === 0 && me.result_position != null;
    const iAmDickTagged = !hasFinished && state?.dick_tagged_player_id != null && String(state.dick_tagged_player_id) === String(playerId);
    myAccoladeEl.textContent = iAmDickTagged ? '🍆' : '';
    myAccoladeEl.classList.toggle('hidden', !iAmDickTagged);
    lastWasMyTurn = isMyTurn;
  }

  function renderTradePile(g, myIdx) {
    pileEl.innerHTML = '';
    const t = g.trading || {};
    const faceDown = t.face_down;
    const highCard = t.high_card;
    const lowCard = t.low_card;
    const epClaimed = t.ep_claimed;
    const shClaimed = t.sh_claimed;
    const me = myIdx >= 0 ? g.players[myIdx] : null;
    const iAmEP = me && me.past_accolade === 'ElPresidente';
    const iAmSH = me && me.past_accolade === 'Shithead';

    const layer = document.createElement('div');
    layer.className = 'pile-layer current trade-cards';

    if (faceDown && t.trade_count) {
      for (let i = 0; i < (t.trade_count || 2); i++) {
        const div = document.createElement('div');
        div.className = 'pile-card card-back';
        div.appendChild(cardBackImg());
        div.style.marginLeft = i === 0 ? '0' : '-31px';
        layer.appendChild(div);
      }
    } else {
      const cardsToShow = [];
      if (highCard && !epClaimed) cardsToShow.push({ card: highCard, role: 'presidente', isMine: iAmEP });
      if (lowCard && !shClaimed) cardsToShow.push({ card: lowCard, role: 'shithead', isMine: iAmSH });
      cardsToShow.sort((a, b) => (a.isMine ? 1 : 0) - (b.isMine ? 1 : 0));
      cardsToShow.forEach((entry, i) => {
        const div = document.createElement('div');
        div.className = 'pile-card trade-card' + (entry.isMine ? ' trade-card-take draggable' : '');
        div.appendChild(cardFrontContent(entry.card));
        div.style.marginLeft = i === 0 ? '0' : '-31px';
        if (entry.isMine) {
          div.draggable = true;
          div.addEventListener('dragstart', (e) => {
            e.dataTransfer.setData('application/json', JSON.stringify({ role: entry.role }));
            e.dataTransfer.effectAllowed = 'move';
            setDragImageFromElement(e, div);
          });
          bindTouchDrag(div, { role: entry.role }, 'trade');
        }
        layer.appendChild(div);
      });
    }

    if (layer.children.length) pileEl.appendChild(layer);
    pileDropZone.classList.toggle('drag-over', false);
  }

  function pileCardRotation(playIdx, cardIdx, card) {
    const h = (playIdx * 7 + cardIdx * 3 + (card.rank || '').charCodeAt(0) + (card.suit || '').charCodeAt(0)) % 51;
    return 10 + h;
  }

  function renderPile(g, isMyTurn) {
    pileEl.innerHTML = '';
    const plays = g.round?.pile?.plays || [];
    const layers = [];
    if (plays.length > 1) {
      const underneath = plays.slice(0, -1);
      underneath.forEach((play, i) => {
        layers.push({ play, cls: 'underneath', rot: (i % 2 === 0 ? -15 : 15) * (i + 1) / 2 });
      });
    }
    if (plays.length > 0) {
      layers.push({ play: plays[plays.length - 1], cls: 'current' });
    }

    layers.forEach(({ play, cls, rot }, playIdx) => {
      const layer = document.createElement('div');
      layer.className = `pile-layer ${cls}`;
      if (rot) layer.style.transform = `rotate(${rot}deg)`;
      const cards = play.cards || [];
      cards.forEach((card, i) => {
        const div = document.createElement('div');
        div.className = 'pile-card';
        div.style.setProperty('--pile-card-rot', pileCardRotation(playIdx, i, card) + 'deg');
        div.appendChild(cardFrontContent(card));
        div.style.marginLeft = i === 0 ? '0' : '-31px';
        layer.appendChild(div);
      });
      pileEl.appendChild(layer);
    });

    if (pendingPlay.length > 0) {
      const layer = document.createElement('div');
      layer.className = 'pile-layer current pending-play';
      pendingPlay.forEach((card, i) => {
        const div = document.createElement('div');
        div.className = 'pile-card draggable';
        div.appendChild(cardFrontContent(card));
        div.style.marginLeft = i === 0 ? '0' : '-31px';
        div.dataset.rank = card.rank;
        div.dataset.suit = card.suit;
        div.draggable = true;
        div.addEventListener('dragstart', (e) => {
          e.dataTransfer.setData('application/json', JSON.stringify([card]));
          e.dataTransfer.effectAllowed = 'move';
          setDragImageFromElement(e, div);
        });
        bindTouchDrag(div, card, 'hand');
        layer.appendChild(div);
      });
      pileEl.appendChild(layer);
    }

    pileDropZone.classList.toggle('drag-over', false);
  }

  function cardsPerRowForWidth(availableWidth) {
    const cardWidth = 73;
    const step = 40.15; /* 10% less horizontal overlap: 73 - 32.85 */
    return Math.max(1, Math.floor((availableWidth - cardWidth) / step) + 1);
  }

  function renderHand(g, myIdx, isMyTurn, validPlays) {
    handEl.innerHTML = '';
    if (myIdx < 0) return;
    const me = g.players[myIdx];
    const hand = me.hand || [];
    const pos = me.result_position;
    const nPlayers = g.players.length;
    const hasFinished = hand.length === 0 && pos != null;

    if (hasFinished) {
      const div = document.createElement('div');
      div.className = 'player-place-result';
      if (pos === 1) {
        div.innerHTML = '<div class="player-place-icon"><img src="/elpres/crown.png" alt="" width="48" height="48"></div><div class="player-place-title">El Presidente</div>';
      } else if (pos === nPlayers) {
        div.innerHTML = '<div class="player-place-icon player-place-poop">💩</div><div class="player-place-title">Shithead</div>';
      } else if (pos === 2) {
        div.innerHTML = '<div class="player-place-icon">⭐</div><div class="player-place-title">VP</div>';
      } else {
        div.innerHTML = '<div class="player-place-rank">#' + pos + '</div>';
      }
      handEl.appendChild(div);
      return;
    }

    const validSets = new Set((validPlays || []).map(vp => vp.map(c => cardKey(c)).sort().join(',')));
    const inPending = new Set(pendingPlay.map(c => cardKey(c)));

    const toShow = [];
    hand.forEach((card, i) => {
      if (inPending.has(cardKey(card))) return;
      toShow.push({ card, i });
    });

    const container = handEl.closest('#player-hand-container') || handEl.parentElement;
    const containerWidth = container ? container.getBoundingClientRect().width : (window.innerWidth - 32);
    const padding = 32;
    const cardsPerRow = cardsPerRowForWidth(Math.max(0, containerWidth - padding));

    let handIndex = 0;
    for (let rowStart = 0; rowStart < toShow.length; rowStart += cardsPerRow) {
      const rowDiv = document.createElement('div');
      rowDiv.className = 'hand-row';
      const rowItems = toShow.slice(rowStart, rowStart + cardsPerRow);
      rowItems.forEach(({ card, i }) => {
        const key = cardKey(card);
        const div = document.createElement('div');
        div.className = 'hand-card' + (cardsFaceDown ? ' cards-face-down' : '');
        div.style.zIndex = handIndex;
        div.appendChild(cardsFaceDown ? cardBackImg() : cardFrontContent(card));
        div.dataset.rank = card.rank;
        div.dataset.suit = card.suit;
        div.dataset.index = i;
        handIndex++;
        const cardObj = card;
        const isPartOfValid = Array.from(validSets).some(setStr => {
          const parts = setStr.split(',');
          return parts.includes(key);
        });
        if (isMyTurn && isPartOfValid) {
          div.classList.add('valid-play', 'draggable');
          div.draggable = true;
          const pileEmpty = (g.round?.pile?.plays || []).length === 0;
          div.addEventListener('dragstart', (e) => {
            if (pileEmpty) startOpeningPlayTimer();
            onDragStart(e, cardObj);
          });
          bindTouchDrag(div, cardObj, 'pile', pileEmpty ? startOpeningPlayTimer : null);
        }
        rowDiv.appendChild(div);
      });
      handEl.appendChild(rowDiv);
    }
  }

  function renderPassButton(g, myIdx, isMyTurn) {
    if (myIdx < 0 || !g.players[myIdx]?.hand?.length) {
      passBtn.style.display = 'none';
      return;
    }
    passBtn.style.display = 'block';
    if (!isMyTurn) {
      passBtn.disabled = true;
      passBtn.textContent = 'Waiting Your Turn';
      passBtn.classList.remove('can-end-turn');
      return;
    }
    const pilePlays = g.round?.pile?.plays || [];
    const pileEmpty = pilePlays.length === 0;
    const mustDefinePlay = pileEmpty && isMyTurn;
    const hasValidPending = pendingPlayMatchesValidPlay(g);
    const lastPlayByMe = g.round?.last_play_player_idx === myIdx && !pileEmpty;

    if (mustDefinePlay) {
      passBtn.textContent = 'Start New Round';
      passBtn.disabled = !hasValidPending;
      passBtn.classList.toggle('can-end-turn', hasValidPending);
    } else if (pendingPlay.length > 0 && !hasValidPending) {
      passBtn.disabled = true;
      passBtn.textContent = lastPlayByMe ? 'Clear the Pile' : 'Pass';
      passBtn.classList.remove('can-end-turn');
    } else if (hasValidPending) {
      passBtn.disabled = false;
      passBtn.textContent = 'Start New Round';
      passBtn.classList.add('can-end-turn');
    } else {
      passBtn.disabled = false;
      passBtn.textContent = lastPlayByMe ? 'Clear the Pile' : 'Pass';
      passBtn.classList.remove('can-end-turn');
    }
  }

  /** True if current selection matches one of the server-provided valid_plays. No game logic here. */
  function pendingPlayMatchesValidPlay(g) {
    const validPlays = g.valid_plays || [];
    if (!pendingPlay.length) return false;
    const pendingKeys = pendingPlay.map(c => cardKey(c)).sort().join(',');
    return validPlays.some((vp) => {
      if (!Array.isArray(vp) || vp.length !== pendingPlay.length) return false;
      const vpKeys = vp.map(c => cardKey(c)).sort().join(',');
      return vpKeys === pendingKeys;
    });
  }

  function accoladeIcon(accolade) {
    if (accolade === 'ElPresidente') return '👑';
    if (accolade === 'Shithead') return '💩';
    if (accolade === 'VP') return '⭐';
    return '';
  }

  function createPlayerStatusDiv(p, players, g) {
    const isCurrentTurn = g.phase === 'Playing' && players.findIndex(pl => pl.id === p.id) === g.current_player_idx;
    const pilePlays = g.round?.pile?.plays || [];
    const lastPlayIdx = g.round?.last_play_player_idx ?? -1;
    const hasHighestPlay = g.phase === 'Playing' && pilePlays.length > 0 && lastPlayIdx >= 0 && players.findIndex(pl => pl.id === p.id) === lastPlayIdx;
    const div = document.createElement('div');
    div.className = 'player-status' + (p.disconnected ? ' player-disconnected' : '') + (isCurrentTurn ? ' player-status-current' : '') + (hasHighestPlay ? ' player-status-highest-play' : '') + ' player-status-tappable';
    const nPlayers = (players || []).length;
    const isDickTagged = state?.dick_tagged_player_id != null && String(state.dick_tagged_player_id) === String(p.id);
    const showPastAccolades = (g.rounds_completed ?? 0) === 0;
    const pastAccIcon = showPastAccolades ? accoladeIcon(p.past_accolade) : '';
    const pos = p.result_position === 1 ? ' 👑' : (p.result_position === nPlayers ? ' 💩' : (p.result_position === 2 ? ' ⭐' : (p.result_position ? ` (#${p.result_position})` : '')));
    const showAccIcon = (p.result_position === 1 && pastAccIcon === '👑') || (p.result_position === nPlayers && pastAccIcon === '💩') || (p.result_position === 2 && pastAccIcon === '⭐') ? '' : pastAccIcon;
    const zzz = p.disconnected ? ' 😴' : '';
    const dickEmoji = isDickTagged ? ' 🍆' : '';
    const pIdx = players.findIndex(pl => pl.id === p.id);
    const inactiveNose = (showInactiveNoseForPlayerIdx !== null && pIdx === showInactiveNoseForPlayerIdx && isCurrentTurn) ? ' 👃' : '';
    const accoladePart = `${pos} ${showAccIcon}${dickEmoji}`;
    const highestPlayDot = hasHighestPlay ? '<div class="player-status-highest-dot"></div>' : '';
    div.innerHTML = `
      <div class="name-row">${highestPlayDot}<div class="name">${escapeHtml(p.name)}${inactiveNose}${accoladePart}${zzz}</div></div>
      <div class="cards-spread-wrapper">
        <div class="cards-spread">${renderMiniCards(p.card_count || 0)}</div>
      </div>
    `;
    div.dataset.playerId = p.id;
    div.addEventListener('click', () => {
      const targetId = String(p.id);
      if (targetId === String(playerId)) return; /* cannot tag yourself */
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'tag_dick', target_player_id: targetId }));
      }
    });
    return div;
  }

  function renderPlayersStatus(g, myIdx) {
    playersEl.innerHTML = '';
    const players = g.players || [];
    const N = players.length;
    if (N === 0) return;

    // Active players: height scales with player count (fewer = shorter). Spectators use CSS 65%.
    const pileTops = [28, 31, 34, 37, 41, 45]; // 2..7 players: top edge of play area (vh)
    const pileEls = [pileCircle, pileCrown, pileEl, pileDropZone];
    if (myIdx >= 0) {
      const heights = [22, 28, 34, 40, 45, 50]; // 2..7 players
      const pct = heights[Math.min(Math.max(N - 2, 0), heights.length - 1)] || 40;
      playersEl.style.height = pct + '%';
      const topPct = pileTops[Math.min(Math.max(N - 2, 0), pileTops.length - 1)] ?? 55;
      const pileTop = `calc(${topPct}vh + 98px)`; // center so top edge is at topPct
      pileEls.forEach((el) => { if (el) el.style.top = pileTop; });
    } else {
      playersEl.style.height = '';
      pileEls.forEach((el) => { if (el) el.style.top = ''; });
    }
    // Active players: N-1 others, clockwise from player to our left. Spectators: all N players.
    let toDisplay;
    if (myIdx < 0) {
      const startIdx = g.current_player_idx >= 0 ? g.current_player_idx : 0;
      toDisplay = Array.from({ length: N }, (_, i) => players[(startIdx + i) % N]);
    } else {
      toDisplay = Array.from({ length: N - 1 }, (_, i) => players[(myIdx + 1 + i) % N]);
    }
    const n = toDisplay.length;
    if (n === 0) return;

    const leftCol = document.createElement('div');
    leftCol.className = 'players-column players-left' + (n % 2 === 0 ? ' players-count-even' : '');
    const topCol = document.createElement('div');
    topCol.className = 'players-column players-top';
    const rightCol = document.createElement('div');
    rightCol.className = 'players-column players-right' + (n % 2 === 0 ? ' players-count-even' : '');

    // Layout: even n => split left/right; odd n => middle at top, rest left/right (matches original N-based logic)
    if (n % 2 === 0) {
      const half = n / 2;
      toDisplay.slice(0, half).forEach((p) => leftCol.appendChild(createPlayerStatusDiv(p, players, g)));
      toDisplay.slice(half).forEach((p) => rightCol.appendChild(createPlayerStatusDiv(p, players, g)));
    } else {
      const mid = Math.floor(n / 2);
      toDisplay.slice(0, mid).forEach((p) => leftCol.appendChild(createPlayerStatusDiv(p, players, g)));
      topCol.appendChild(createPlayerStatusDiv(toDisplay[mid], players, g));
      toDisplay.slice(mid + 1).forEach((p) => rightCol.appendChild(createPlayerStatusDiv(p, players, g)));
    }

    if (leftCol.childNodes.length) playersEl.appendChild(leftCol);
    if (topCol.childNodes.length) playersEl.appendChild(topCol);
    if (rightCol.childNodes.length) playersEl.appendChild(rightCol);
  }

  function renderMiniCards(count) {
    const n = Math.max(0, count);
    const cardW = 29;  // 22 * 1.3
    const cardH = 39;  // 30 * 1.3
    const totalAngle = 42;
    const positions = Array(n).fill(0).map((_, i) => {
      let leftPx = 0;
      for (let j = 0; j < i; j++) {
        const oj = n <= 1 ? 0.9 : 0.9 - (0.2 * j) / (n - 1);
        leftPx += cardW * (1 - oj);
      }
      return leftPx;
    });
    const spanWidth = n === 0 ? 0 : n === 1 ? cardW : positions[n - 1] + cardW;
    const halfAngleRad = (totalAngle / 2) * Math.PI / 180;
    const cornerExtend = n <= 1 ? 0 : cardH * Math.sin(halfAngleRad) - (cardW / 2) * (1 - Math.cos(halfAngleRad));
    const rotationOverflow = Math.ceil(Math.max(0, cornerExtend));
    const totalWidth = spanWidth + 2 * rotationOverflow;
    const offset = cornerExtend - rotationOverflow;
    const cardsHtml = Array(n).fill(0).map((_, i) => {
      const rot = n <= 1 ? 0 : -totalAngle / 2 + (totalAngle * i) / Math.max(1, n - 1);
      const left = positions[i] + offset + rotationOverflow;
      const style = [
        `z-index: ${i}`,
        `transform-origin: ${cardW / 2}px ${cardH}px`,
        `transform: rotate(${rot}deg)`,
        `left: ${left}px`
      ].join('; ');
      return `<div class="mini-card" style="${style}"><img src="/elpres/cards/back.svg" alt="" class="mini-card-back"></div>`;
    }).join('');
    return n === 0 ? '' : `<div class="cards-spread-inner" style="width: ${totalWidth}px">${cardsHtml}</div>`;
  }

  function escapeHtml(s) {
    const d = document.createElement('div');
    d.textContent = s;
    return d.innerHTML;
  }

  function setDragImageFromElement(e, el) {
    const ghost = el.cloneNode(true);
    ghost.classList.add('drag-ghost');
    ghost.style.position = 'absolute';
    ghost.style.left = '-9999px';
    document.body.appendChild(ghost);
    e.dataTransfer.setDragImage(ghost, ghost.offsetWidth / 2, ghost.offsetHeight / 2);
    setTimeout(() => ghost.remove(), 0);
  }

  function onDragStart(e, card) {
    e.dataTransfer.setData('application/json', JSON.stringify([card]));
    e.dataTransfer.effectAllowed = 'move';
    e.target.classList.add('dragging');
    setDragImageFromElement(e, e.target);
  }

  /** Touch-drag polyfill: on touchend over dropTarget, perform the same action as drag-and-drop. payload is cards array for 'pile'/'hand', or { role } for 'trade'. Shows a moving ghost during drag. Optional onDragStartCb when drag begins. */
  function bindTouchDrag(el, payload, dropTarget, onDragStartCb) {
    const cardsArr = Array.isArray(payload) ? payload : (payload && payload.role ? null : [payload]);
    let touchDragActive = false;
    let startX = 0, startY = 0;
    let ghost = null;

    function updateGhost(x, y) {
      if (!ghost) return;
      ghost.style.left = x + 'px';
      ghost.style.top = y + 'px';
    }

    function removeGhost() {
      if (ghost && ghost.parentNode) ghost.parentNode.removeChild(ghost);
      ghost = null;
    }

    el.addEventListener('touchstart', (e) => {
      if (e.touches.length !== 1) return;
      touchDragActive = false;
      startX = e.touches[0].clientX;
      startY = e.touches[0].clientY;
    }, { passive: true });
    el.addEventListener('touchmove', (e) => {
      if (e.touches.length !== 1) return;
      if (!touchDragActive) {
        const dx = e.touches[0].clientX - startX;
        const dy = e.touches[0].clientY - startY;
        if (dx * dx + dy * dy > 100) {
          touchDragActive = true;
          if (typeof onDragStartCb === 'function') onDragStartCb();
          el.classList.add('dragging');
          ghost = el.cloneNode(true);
          ghost.classList.add('drag-ghost');
          ghost.style.left = e.touches[0].clientX + 'px';
          ghost.style.top = e.touches[0].clientY + 'px';
          document.body.appendChild(ghost);
        }
      }
      if (touchDragActive) {
        if (e.cancelable) e.preventDefault();
        updateGhost(e.touches[0].clientX, e.touches[0].clientY);
        const under = document.elementFromPoint(e.touches[0].clientX, e.touches[0].clientY);
        if (dropTarget === 'pile') {
          pileDropZone.classList.toggle('drag-over', under && (pileContainer.contains(under) || pileEl.contains(under)));
        } else if (dropTarget === 'hand' || dropTarget === 'trade') {
          handContainer.classList.toggle('hand-drag-over', under && handContainer.contains(under));
        }
      }
    }, { passive: false });
    el.addEventListener('touchend', (e) => {
      if (!touchDragActive) return;
      el.classList.remove('dragging');
      removeGhost();
      pileDropZone.classList.remove('drag-over');
      handContainer.classList.remove('hand-drag-over');
      touchDragActive = false;
      const t = e.changedTouches && e.changedTouches[0];
      if (!t) return;
      const under = document.elementFromPoint(t.clientX, t.clientY);
      if (!under) return;
      if (dropTarget === 'pile' && (pileContainer.contains(under) || pileEl.contains(under)) && cardsArr) {
        addCardsToPileDrop(cardsArr);
      }
      if (dropTarget === 'trade' && handContainer.contains(under) && payload && (payload.role === 'presidente' || payload.role === 'shithead')) {
        if (ws && ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({ type: 'claim_trade', role: payload.role }));
        }
      }
      if (dropTarget === 'hand' && handContainer.contains(under) && cardsArr) {
        cardsArr.forEach(c => {
          const idx = pendingPlay.findIndex(p => p.rank === c.rank && p.suit === c.suit);
          if (idx >= 0) pendingPlay.splice(idx, 1);
        });
        clearOpeningPlayTimer();
        render();
      }
    }, { passive: true });
    el.addEventListener('touchcancel', () => {
      el.classList.remove('dragging');
      removeGhost();
      touchDragActive = false;
    }, { passive: true });
  }

  function clearOpeningPlayTimer() {
    if (openingPlayTimerId) {
      clearTimeout(openingPlayTimerId);
      openingPlayTimerId = null;
    }
  }

  function clearTurnPassTimer() {
    if (turnTimerPhase1Id) {
      clearTimeout(turnTimerPhase1Id);
      turnTimerPhase1Id = null;
    }
    if (turnTimerPhase2Id) {
      clearTimeout(turnTimerPhase2Id);
      turnTimerPhase2Id = null;
    }
    if (turnCountdownIntervalId) {
      clearInterval(turnCountdownIntervalId);
      turnCountdownIntervalId = null;
    }
    if (autoPassOverlay) autoPassOverlay.classList.add('hidden');
  }

  function startTurnPassTimer() {
    if (turnTimerPhase1Id) return; /* already running */
    turnTimerPhase1Id = setTimeout(() => {
      turnTimerPhase1Id = null;
      if (autoPassOverlay && autoPassMessage && autoPassCountdown) {
        autoPassMessage.textContent = 'You will automatically pass in…';
        let countdown = 30;
        autoPassCountdown.textContent = String(countdown);
        autoPassOverlay.classList.remove('hidden');
        turnCountdownIntervalId = setInterval(() => {
          countdown--;
          autoPassCountdown.textContent = String(Math.max(0, countdown));
          if (countdown <= 0 && turnCountdownIntervalId) {
            clearInterval(turnCountdownIntervalId);
            turnCountdownIntervalId = null;
          }
        }, 1000);
      }
      turnTimerPhase2Id = setTimeout(() => {
        turnTimerPhase2Id = null;
        clearTurnPassTimer();
        if (ws && ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({ type: 'pass' }));
        }
        if (state) render();
      }, TURN_AUTO_PASS_AFTER_MS);
    }, TURN_WARN_AFTER_MS);
  }

  function startOpeningPlayTimer() {
    if (!state || state.phase !== 'Playing') return;
    const g = state;
    const myIdx = (g.players || []).findIndex(p => p.id === playerId);
    if (g.current_player_idx !== myIdx) return;
    const pilePlays = g.round?.pile?.plays || [];
    if (pilePlays.length !== 0) return; /* not opening play */
    clearOpeningPlayTimer();
    openingPlayTimerId = setTimeout(() => {
      openingPlayTimerId = null;
      if (!state || state.phase !== 'Playing') return;
      if (pendingPlay.length === 0) return; /* user dragged cards back off pile */
      const gg = state;
      const idx = (gg.players || []).findIndex(p => p.id === playerId);
      if (gg.current_player_idx !== idx) return;
      const plays = gg.round?.pile?.plays || [];
      if (plays.length !== 0) return; /* no longer opening play */
      if (!ws || ws.readyState !== WebSocket.OPEN) return;
      clearTurnPassTimer();
      const hasValid = pendingPlayMatchesValidPlay(gg);
      if (hasValid && pendingPlay.length > 0) {
        ws.send(JSON.stringify({ type: 'play', cards: pendingPlay }));
        pendingPlay.length = 0;
      } else {
        ws.send(JSON.stringify({ type: 'pass' }));
      }
      render();
    }, OPENING_PLAY_TIMEOUT_MS);
  }

  /** If pending play is valid and it's our turn, send play and clear pending (auto-end turn). Skip when pile is empty (first play of round) unless they're playing their last card. */
  function trySubmitPlayIfValid() {
    if (!state || state.phase !== 'Playing') return;
    const myIdx = (state.players || []).findIndex(p => p.id === playerId);
    if (state.current_player_idx !== myIdx) return;
    if (!pendingPlay.length || !pendingPlayMatchesValidPlay(state)) return;
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    const pilePlays = state.round?.pile?.plays || [];
    const myHand = (state.players && state.players[myIdx] && state.players[myIdx].hand) ? state.players[myIdx].hand : [];
    const isPlayingLastCard = pendingPlay.length === myHand.length;
    if (pilePlays.length === 0 && !isPlayingLastCard) return; /* first play of round: require explicit End My Turn unless playing last card */
    clearTurnPassTimer();
    ws.send(JSON.stringify({ type: 'play', cards: pendingPlay.slice() }));
    pendingPlay.length = 0;
    render();
  }

  function addCardsToPileDrop(cards) {
    if (!Array.isArray(cards) || !cards.length) return;
    const existingKeys = new Set(pendingPlay.map(c => cardKey(c)));
    let added = 0;
    for (const c of cards) {
      if (!existingKeys.has(cardKey(c))) {
        pendingPlay.push(c);
        existingKeys.add(cardKey(c));
        added++;
      }
    }
    if (added) {
      const pilePlays = state?.round?.pile?.plays || [];
      if (pilePlays.length === 0) startOpeningPlayTimer();
      render();
      setTimeout(function refreshPassButton() {
        if (!state || state.phase === 'no_game') return;
        const g = state;
        const myIdx = (g.players || []).findIndex(p => p.id === playerId);
        const isMyTurn = g.current_player_idx === myIdx && g.phase === 'Playing';
        renderPassButton(g, myIdx, isMyTurn);
      }, 0);
      trySubmitPlayIfValid();
    }
  }

  function handlePileDrop(e) {
    e.preventDefault();
    pileDropZone.classList.remove('drag-over');
    try {
      const data = e.dataTransfer.getData('application/json');
      if (data) addCardsToPileDrop(JSON.parse(data));
    } catch (_) {}
  }

  function setupDropZone() {
    const onDragover = (e) => {
      e.preventDefault();
      e.dataTransfer.dropEffect = 'move';
      if (!state || state.phase !== 'Playing') return;
      const myIdx = (state.players || []).findIndex(p => p.id === playerId);
      if (state.current_player_idx !== myIdx) return;
      pileDropZone.classList.add('drag-over');
    };
    pileContainer.addEventListener('dragover', onDragover);
    pileEl.addEventListener('dragover', onDragover);
    pileContainer.addEventListener('dragleave', (e) => {
      if (!pileContainer.contains(e.relatedTarget)) {
        pileDropZone.classList.remove('drag-over');
      }
    });
    pileContainer.addEventListener('drop', handlePileDrop);
  }

  function setupHandDropZone() {
    handContainer.addEventListener('dragover', (e) => {
      e.preventDefault();
      e.dataTransfer.dropEffect = 'move';
      const trading = state?.phase === 'Trading' && state?.trading;
      if (trading) {
        handContainer.classList.add('hand-drag-over');
      } else if (pendingPlay.length > 0) {
        handContainer.classList.add('hand-drag-over');
      }
    });
    handContainer.addEventListener('dragleave', (e) => {
      if (!handContainer.contains(e.relatedTarget)) {
        handContainer.classList.remove('hand-drag-over');
      }
    });
    handContainer.addEventListener('drop', (e) => {
      e.preventDefault();
      handContainer.classList.remove('hand-drag-over');
      try {
        const data = e.dataTransfer.getData('application/json');
        if (!data) return;
        const parsed = JSON.parse(data);
        if (state?.phase === 'Trading' && parsed && (parsed.role === 'presidente' || parsed.role === 'shithead')) {
          if (ws && ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({ type: 'claim_trade', role: parsed.role }));
          }
          return;
        }
        if (Array.isArray(parsed)) {
          const cards = parsed;
          cards.forEach(c => {
            const idx = pendingPlay.findIndex(p => p.rank === c.rank && p.suit === c.suit);
            if (idx >= 0) pendingPlay.splice(idx, 1);
          });
          clearOpeningPlayTimer();
          render();
        }
      } catch (_) {}
    });
  }
  setupHandDropZone();

  document.addEventListener('dragend', () => {
    document.querySelectorAll('.hand-card.dragging').forEach(el => el.classList.remove('dragging'));
    handContainer.classList.remove('hand-drag-over');
    pileDropZone.classList.remove('drag-over');
  });

  if (autoPassOverlay) {
    autoPassOverlay.addEventListener('pointerdown', () => {
      if (!autoPassOverlay.classList.contains('hidden')) {
        clearTurnPassTimer();
        startTurnPassTimer(); /* restart the 30s cycle */
      }
    });
  }

  document.addEventListener('pointerdown', (e) => {
    if (!state || state.phase !== 'Playing' || state.spectator === true) return;
    const myIdx = (state.players || []).findIndex(p => String(p.id) === String(playerId));
    if (myIdx < 0 || state.current_player_idx !== myIdx) return;
    if (passBtn && e.target && passBtn.contains(e.target)) return;
    if (autoPassOverlay && !autoPassOverlay.classList.contains('hidden') && e.target && autoPassOverlay.contains(e.target)) return;
    clearTurnPassTimer();
    startTurnPassTimer();
  });

  passBtn.addEventListener('click', () => {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    clearTurnPassTimer();
    const g = state?.phase !== 'no_game' ? state : null;
    const hasValid = g ? pendingPlayMatchesValidPlay(g) : false;
    if (hasValid && pendingPlay.length > 0) {
      ws.send(JSON.stringify({ type: 'play', cards: pendingPlay }));
      pendingPlay.length = 0;
    } else {
      ws.send(JSON.stringify({ type: 'pass' }));
    }
    render();
  });

  spectatorToggleBtn.addEventListener('click', () => {
    if (!ws || ws.readyState !== WebSocket.OPEN || state.spectator !== true) return;
    const wantsToPlayCurrent = state.wants_to_play !== false;
    ws.send(JSON.stringify({ type: 'spectator_preference', want_to_play: !wantsToPlayCurrent }));
  });

  startGameBtn.addEventListener('click', () => {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: 'start_game' }));
    }
  });

  setupDropZone();
  window.addEventListener('resize', () => { if (state) render(); });
  connect();
})();
