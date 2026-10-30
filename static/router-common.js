(() => {
  const operationPollTimeoutMs = 6 * 60 * 1000;

  window.routerHttp = {
    async postForm(url, formData, options = {}) {
      const response = await fetch(url, {
        method: "POST",
        body: formData,
        credentials: "same-origin",
        headers: { "X-Requested-With": "fetch" },
      });
      if (!response.ok) {
        // Keep the server's explanation (validation / lockout warnings) instead
        // of replacing it with a generic "HTTP 400".
        let detail = "";
        try {
          const body = await response.json();
          detail = body && body.message ? String(body.message) : "";
        } catch {
          detail = "";
        }
        const error = new Error(detail || `HTTP ${response.status}`);
        if (detail) error.serverMessage = detail;
        throw error;
      }
      const result = await response.json();
      if (!result.pending || !result.operation_url) return result;

      const deadline = Date.now() + operationPollTimeoutMs;
      while (true) {
        if (Date.now() >= deadline) {
          throw new Error("Operation polling timed out");
        }
        await new Promise((resolve) => window.setTimeout(resolve, 1000));
        const operationResponse = await fetch(result.operation_url, {
          credentials: "same-origin",
          headers: { "X-Requested-With": "fetch" },
        });
        if (!operationResponse.ok) {
          throw new Error(`HTTP ${operationResponse.status}`);
        }
        const operation = await operationResponse.json();
        if (
          operation.pending
          && operation.progress_message
          && typeof options.onProgress === "function"
        ) {
          options.onProgress(operation.progress_message);
        }
        if (!operation.pending) return operation;
      }
    },
  };

  window.routerFeedback = {
    scoped(key) {
      return (message, type = "success", autoHideMs = 0) => {
        if (!window.routerToast) return;
        if (!message) {
          window.routerToast.clear(key);
          return;
        }
        window.routerToast.show(message, type, { key, autoHideMs });
      };
    },
  };

  // Buttons that kicked off a slow operation show a spinner instead of going
  // silently grey, so a multi-second apply never looks like a dead click.
  window.routerBusy = {
    set(button, busy, label) {
      if (!button) return;
      if (busy) {
        if (button.dataset.idleHtml === undefined) {
          button.dataset.idleHtml = button.innerHTML;
          button.dataset.idleLabel = button.textContent.trim();
        }
        button.disabled = true;
        button.classList.add("is-busy");
        button.setAttribute("aria-busy", "true");
        const spinner = document.createElement("span");
        spinner.className = "button-spinner";
        spinner.setAttribute("aria-hidden", "true");
        const text = document.createElement("span");
        text.textContent = label || button.dataset.idleLabel || "处理中…";
        button.replaceChildren(spinner, text);
      } else {
        button.disabled = false;
        button.classList.remove("is-busy");
        button.removeAttribute("aria-busy");
        if (button.dataset.idleHtml !== undefined) {
          button.innerHTML = button.dataset.idleHtml;
        }
      }
    },
  };

  document.querySelectorAll("[data-agent-operation-form]").forEach((form) => {
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const button = form.querySelector('button[type="submit"]');
      const busyLabel = form.dataset.busyLabel || "正在处理…";
      const notify = (message, type, autoHideMs) => {
        if (!window.routerToast) return;
        window.routerToast.show(message, type, { key: "agent-operation", autoHideMs });
      };

      window.routerBusy.set(button, true, busyLabel);
      form.setAttribute("aria-busy", "true");
      notify(busyLabel, "pending", 0);
      try {
        const result = await window.routerHttp.postForm(
          form.getAttribute("action"),
          new FormData(form),
          {
            onProgress(message) {
              notify(`正在${message}…`, "pending", 0);
            },
          },
        );
        notify(result.message || "操作已结束", result.ok ? "success" : "error", result.ok ? 2400 : 0);
        if (result.ok) {
          // Let the result stay readable for a moment before the page refreshes.
          window.setTimeout(() => window.location.reload(), 1400);
        } else {
          // Keep the error on screen and let the user fix the form in place.
          window.routerBusy.set(button, false);
        }
      } catch (error) {
        notify(error?.serverMessage || "系统操作失败，请稍后重试", "error", 0);
        window.routerBusy.set(button, false);
      } finally {
        form.removeAttribute("aria-busy");
      }
    });
  });
})();

// Show only the fields that belong to the selected WAN mode (DHCP vs PPPoE) and
// keep the LAN port checkboxes in sync with whichever NIC currently carries WAN.
(function () {
  const modeSelector = document.querySelector("[data-wan-mode]");
  const wanSelector = document.querySelector("[data-wan-interface]");
  const pppoeSelector = document.querySelector("[data-pppoe-nic]");
  const modeFields = document.querySelectorAll("[data-wan-field]");
  const portBoxes = Array.from(document.querySelectorAll("[data-lan-port]"));
  if (!modeSelector && !wanSelector && !pppoeSelector) return;

  // Remember what the user actually wanted for each port, so moving the uplink
  // to another NIC restores the previous LAN selection instead of dropping it.
  const wanted = new Map();
  portBoxes.forEach((box) => {
    wanted.set(box.dataset.lanPort, box.checked);
    // The server renders the current uplink as disabled; adopt it so it can be
    // released again once the uplink moves to another NIC.
    if (box.disabled) box.dataset.uplinkLocked = "1";
  });

  const currentMode = () => (modeSelector ? modeSelector.value : "dhcp");
  const currentUplink = () => {
    const selector = currentMode() === "pppoe" ? pppoeSelector : wanSelector;
    return selector ? selector.value.trim() : "";
  };

  const syncFields = () => {
    const mode = currentMode();
    modeFields.forEach((group) => {
      group.hidden = group.dataset.wanField !== mode;
    });
  };

  const syncPorts = () => {
    const uplink = currentUplink();
    portBoxes.forEach((box) => {
      const name = box.dataset.lanPort;
      const option = box.closest(".checkbox-option");
      if (uplink && name === uplink) {
        if (!box.dataset.uplinkLocked) {
          wanted.set(name, box.checked);
          box.dataset.uplinkLocked = "1";
        }
        box.checked = false;
        box.disabled = true;
        box.title = "该网口正在作为 WAN 使用，不能同时作为 LAN";
        option?.classList.add("is-uplink-locked");
      } else if (box.dataset.uplinkLocked) {
        delete box.dataset.uplinkLocked;
        box.disabled = false;
        box.checked = Boolean(wanted.get(name));
        box.removeAttribute("title");
        option?.classList.remove("is-uplink-locked");
      }
    });
  };

  const sync = () => {
    syncFields();
    syncPorts();
  };

  [modeSelector, wanSelector, pppoeSelector].forEach((element) => {
    element?.addEventListener("change", sync);
  });
  portBoxes.forEach((box) => {
    box.addEventListener("change", () => {
      if (!box.dataset.uplinkLocked) wanted.set(box.dataset.lanPort, box.checked);
    });
  });
  sync();
})();

