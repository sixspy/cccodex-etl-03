const { createApp } = Vue;

createApp({
  data() {
    return {
      me: "",
      items: [],
      board: { settle_day: "", totals: {}, items: [] },
      current: null,
      settings: { daily_quota: 0, overflow_path: "fail" },
      busy: false,
      msg: "",
      settingsMsg: "",
    };
  },
  computed: {
    canContinue() {
      if (!this.current) return false;
      const day = this.current.settle_day;
      return (this.current.candidates || []).some(
        (c) => c.status === "waiting" && c.created_day < day
      );
    },
    pathLocked() {
      return !!(this.current && this.current.path_locked_today);
    },
  },
  async mounted() {
    const me = await fetch("/api/me");
    if (!me.ok) {
      location.href = "/login";
      return;
    }
    this.me = (await me.json()).user;
    await this.refreshAll();
  },
  methods: {
    tagClass(status) {
      if (status === "已落库") return "ok";
      if (status === "已抽取待加载" || status === "部分落库") return "warn";
      return "";
    },
    async refreshAll() {
      const [list, board] = await Promise.all([
        fetch("/api/pipelines"),
        fetch("/api/quota/board"),
      ]);
      if (list.ok) this.items = (await list.json()).items || [];
      if (board.ok) this.board = await board.json();
    },
    async fetchDetail(id) {
      const res = await fetch(`/api/pipelines/${id}`);
      if (!res.ok) return;
      this.current = await res.json();
      this.settings = {
        daily_quota: this.current.daily_quota,
        overflow_path: this.current.overflow_path || "fail",
      };
    },
    async open(id) {
      await this.fetchDetail(id);
      this.msg = "";
      this.settingsMsg = "";
    },
    async extract() {
      if (!this.current) return;
      this.busy = true;
      this.msg = "";
      try {
        const res = await fetch(`/api/pipelines/${this.current.id}/extract`, {
          method: "POST",
        });
        const data = await res.json();
        if (!res.ok) {
          this.msg = data.detail || "抽取失败";
        } else {
          this.current = data;
          this.msg = "抽取完成，可继续落库";
        }
        await this.refreshAll();
      } finally {
        this.busy = false;
      }
    },
    async load() {
      if (!this.current) return;
      this.busy = true;
      this.msg = "";
      try {
        const res = await fetch(`/api/pipelines/${this.current.id}/load`, {
          method: "POST",
        });
        const data = await res.json();
        if (!res.ok) {
          this.msg = data.detail || "落库失败";
          await this.fetchDetail(this.current.id);
        } else {
          this.current = data;
          this.msg =
            data.status === "部分落库"
              ? `配额不足，已落 ${data.loaded} 条，超额 ${data.candidates_pending_rows} 条进候补池`
              : "落库完成";
        }
        await this.refreshAll();
      } finally {
        this.busy = false;
      }
    },
    async continuePool() {
      if (!this.current) return;
      this.busy = true;
      this.msg = "";
      try {
        const res = await fetch(
          `/api/pipelines/${this.current.id}/candidates/continue`,
          { method: "POST" }
        );
        const data = await res.json();
        if (!res.ok) {
          this.msg = data.detail || "候补继续失败";
          await this.fetchDetail(this.current.id);
        } else {
          this.current = data;
          this.msg =
            data.candidates_pending_rows > 0
              ? `已按新日配额续落，仍余 ${data.candidates_pending_rows} 条候补`
              : "候补已全部落库";
        }
        await this.refreshAll();
      } finally {
        this.busy = false;
      }
    },
    async saveSettings() {
      if (!this.current) return;
      this.busy = true;
      this.settingsMsg = "";
      try {
        const res = await fetch(`/api/pipelines/${this.current.id}/quota`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            daily_quota: this.settings.daily_quota,
            overflow_path: this.settings.overflow_path,
          }),
        });
        const data = await res.json();
        if (!res.ok) {
          this.settingsMsg = data.detail || "保存失败";
          await this.fetchDetail(this.current.id);
        } else {
          this.current = data;
          this.settingsMsg = "设置已写进说明";
        }
        await this.refreshAll();
      } finally {
        this.busy = false;
      }
    },
    async logout() {
      await fetch("/api/logout", { method: "POST" });
      location.href = "/login";
    },
  },
}).mount("#app");
