<script setup>
import { computed, onMounted, ref } from 'vue'
import { getJson, postJson } from '../api'

const rows = ref([])
const revocations = ref([])
const err = ref('')
const msg = ref('')
const busyKey = ref('')

async function load() {
  const [entries, revs] = await Promise.all([
    getJson('/api/entries?kind=冲正'),
    getJson('/api/revocations'),
  ])
  rows.value = entries
  revocations.value = revs
}

onMounted(async () => {
  try {
    await load()
  } catch (e) {
    err.value = String(e.message || e)
  }
})

const revokedByKey = computed(() => {
  const m = {}
  for (const r of revocations.value) m[r.reversal_key] = r
  return m
})

async function revoke(entry) {
  busyKey.value = entry.entry_key
  err.value = ''
  msg.value = ''
  try {
    const r = await postJson('/api/revocations', { reversal_key: entry.entry_key })
    msg.value =
      `已撤销 ${r.reversal_key}：恢复 ${(r.restored_cents / 100).toFixed(2)} 元，` +
      `账户余额 ${(r.balance_before_cents / 100).toFixed(2)} → ${(r.balance_after_cents / 100).toFixed(2)}，` +
      `原单剩余敞口 ${(r.exposure_before_cents / 100).toFixed(2)} → ${(r.exposure_after_cents / 100).toFixed(2)}`
    await load()
  } catch (e) {
    err.value = String(e.message || e)
  } finally {
    busyKey.value = ''
  }
}

const proof = ref(null)
const proofErr = ref('')

async function loadProof(id) {
  proof.value = null
  proofErr.value = ''
  try {
    proof.value = await getJson(`/api/revocations/${id}/proof`)
  } catch (e) {
    proofErr.value = String(e.message || e)
  }
}

function yuan(cents) {
  return (Number(cents || 0) / 100).toFixed(2)
}
</script>

<template>
  <div>
    <h1>冲正流水</h1>
    <p class="lead">
      冲正须关联原正向流水键；无原单或超额会在归并时进未平隔离。
      仅「已归并」冲正可发起整笔撤销：撤销后写入撤销事件，状态变为已撤销。
    </p>
    <p v-if="msg" class="ok">{{ msg }}</p>
    <p v-if="err" class="msg">{{ err }}</p>
    <div class="panel">
      <table>
        <thead>
          <tr>
            <th>流水键</th><th>账户</th><th>金额（元）</th><th>原单键</th><th>业务单</th><th>状态</th><th>撤销证据</th><th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="e in rows" :key="e.id">
            <td>{{ e.entry_key }}</td>
            <td>{{ e.account_code }}</td>
            <td>{{ yuan(e.amount_cents) }}</td>
            <td>{{ e.target_entry_key || '—' }}</td>
            <td>{{ e.biz_ref }}</td>
            <td>
              <span
                class="tag"
                :class="{
                  on: e.status === '已归并',
                  bad: e.status === '已隔离',
                  void: e.status === '已撤销',
                }"
              >{{ e.status }}</span>
            </td>
            <td class="evidence">
              <template v-if="revokedByKey[e.entry_key]">
                <span class="mono">恢复 {{ yuan(revokedByKey[e.entry_key].restored_cents) }}</span>
                <span class="mono dim">
                  敞口 {{ yuan(revokedByKey[e.entry_key].exposure_before_cents) }} → {{ yuan(revokedByKey[e.entry_key].exposure_after_cents) }}
                </span>
                <span class="mono dim">
                  余额 {{ yuan(revokedByKey[e.entry_key].balance_before_cents) }} → {{ yuan(revokedByKey[e.entry_key].balance_after_cents) }}
                </span>
                <span class="mono dim">{{ revokedByKey[e.entry_key].revoked_at }}</span>
                <span class="mono dim">{{ revokedByKey[e.entry_key].run_id }}</span>
              </template>
              <span v-else class="dim">—</span>
            </td>
            <td>
              <button
                v-if="e.status === '已归并'"
                class="btn secondary small"
                :disabled="busyKey === e.entry_key"
                @click="revoke(e)"
              >整笔撤销</button>
              <span v-else-if="e.status === '已撤销'" class="dim">已撤销</span>
              <span v-else class="dim">不可撤销</span>
            </td>
          </tr>
          <tr v-if="!rows.length"><td colspan="8">暂无冲正流水</td></tr>
        </tbody>
      </table>
    </div>

    <div class="panel">
      <h2>撤销记录与恢复证明</h2>
      <table>
        <thead>
          <tr>
            <th>撤销#</th><th>冲正键</th><th>原单键</th><th>账户</th><th>恢复额（元）</th>
            <th>敞口前（元）</th><th>敞口后（元）</th><th>余额前（元）</th><th>余额后（元）</th>
            <th>运行 id</th><th>时刻</th><th>证明</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="r in revocations" :key="r.id">
            <td>{{ r.id }}</td>
            <td class="mono">{{ r.reversal_key }}</td>
            <td class="mono">{{ r.target_entry_key }}</td>
            <td>{{ r.account_code }}</td>
            <td>{{ yuan(r.restored_cents) }}</td>
            <td>{{ yuan(r.exposure_before_cents) }}</td>
            <td>{{ yuan(r.exposure_after_cents) }}</td>
            <td>{{ yuan(r.balance_before_cents) }}</td>
            <td>{{ yuan(r.balance_after_cents) }}</td>
            <td class="mono">{{ r.run_id }}</td>
            <td class="mono">{{ r.revoked_at }}</td>
            <td>
              <button class="btn secondary small" @click="loadProof(r.id)">查看</button>
            </td>
          </tr>
          <tr v-if="!revocations.length"><td colspan="12">暂无撤销记录</td></tr>
        </tbody>
      </table>
      <p v-if="proofErr" class="msg">{{ proofErr }}</p>
      <div v-if="proof" class="proof-block">
        <h3>恢复证明 #{{ proof.proof_id }}（撤销事件 #{{ proof.revocation_id }}）</h3>
        <dl>
          <dt>冲正键</dt><dd class="mono">{{ proof.reversal_key }}</dd>
          <dt>原单键</dt><dd class="mono">{{ proof.target_entry_key }}</dd>
          <dt>恢复额（元）</dt><dd>{{ yuan(proof.restored_cents) }}</dd>
          <dt>敞口前 → 后（元）</dt>
          <dd>{{ yuan(proof.exposure_before_cents) }} → {{ yuan(proof.exposure_after_cents) }}</dd>
          <dt>余额前 → 后（元）</dt>
          <dd>{{ yuan(proof.balance_before_cents) }} → {{ yuan(proof.balance_after_cents) }}</dd>
          <dt>运行 id</dt><dd class="mono">{{ proof.run_id }}</dd>
          <dt>时刻</dt><dd class="mono">{{ proof.proof_created_at }}</dd>
        </dl>
      </div>
    </div>
  </div>
</template>
