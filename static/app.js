async function rpc(cmd, payload) {
  const res = await fetch(`/rpc/${cmd}`, {
    method: 'POST',
    headers: {
      accept: 'application/json',
      'content-type': 'application/json',
    },
    body: JSON.stringify(payload || {}),
  });
  if (res.status === 204) return;
  const result = await res.json();
  if (res.status > 300) {
    throw {
      status: res.status,
      result,
    };
  }
  return result.data;
}

async function query() {
  const data = await rpc('query');
  return data;
}

async function delay(time) {
  return new Promise(resolve => setTimeout(resolve, time));
}

new Vue({
  data: {
    servers: null,
    modalAdd: null,
  },
  methods: {
    formatTransfer(bytes) {
      if (isNaN(bytes)) return '-';
      const formatFraction = num => {
        if (num < 10) return num.toFixed(2);
        return Math.floor(num);
      };
      let kb = bytes / 1024;
      if (kb < 100) return formatFraction(kb) + 'KB';
      const mb = kb / 1024;
      if (mb < 100) return formatFraction(mb) + 'MB';
      return formatFraction(mb / 1024) + 'GB';
    },
    async refreshData() {
      while (true) {
        try {
          const data = await query();
          this.servers = data.config.servers.map(item => ({
            ...item,
            meta: data.meta[item.key] || {},
          }));
        } catch {
          // noop
        }
        await delay(2000);
      }
    },
    onAdd() {
      this.modalAdd = {
        text: '',
      };
    },
    async onConfirmAdd() {
      const config = JSON.parse(this.modalAdd.text);
      await rpc('add', { config });
      this.modalAdd = null;
    },
    async onToggle(key, enabled) {
      await rpc('toggle', { key, enabled });
    },
    async onRemove(key) {
      await rpc('remove', { key });
    },
  },
  async created() {
    this.refreshData();
  },
}).$mount('.container');
