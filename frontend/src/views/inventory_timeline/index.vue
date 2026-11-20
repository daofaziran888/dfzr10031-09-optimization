<template>
  <section class="page" data-module="inventory_timeline">
    <header class="page-head">
      <div>
        <h2>库存时点跨表查询</h2>
        <p class="page-desc">
          接收门店商品、时区、业务时点与带有效区间的库存调整，按门店日界定位每个查询时点的有效库存；
          支持复盘（系统收到时刻）与实际（业务时点）双视角。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openBatchModal">登记盘点批次</button>
      </div>
    </header>

    <nav class="tab-bar">
      <button
        v-for="tab in tabs"
        :key="tab.key"
        class="btn"
        :class="{ primary: activeTab === tab.key }"
        type="button"
        @click="switchTab(tab.key)"
      >
        {{ tab.label }}
      </button>
    </nav>

    <!--  Tab 1：库存时点集市  -->
    <div v-if="activeTab === 'mart'">
      <div class="stat-row">
        <article class="stat-card">
          <span class="stat-label">门店商品行数</span>
          <strong class="stat-value">{{ summary['门店商品行数'] ?? total }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">可用库存合计</span>
          <strong class="stat-value">{{ summary['可用库存合计'] ?? 0 }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">缺记录商品总数</span>
          <strong class="stat-value">{{ summary['缺记录商品总数'] ?? 0 }}</strong>
        </article>
      </div>

      <form class="filter-bar" @submit.prevent="reloadMart(1)">
        <label class="filter-item">
          <span>门店</span>
          <select v-model="filters.store_id">
            <option value="">全部门店</option>
            <option v-for="s in stores" :key="s['门店编号']" :value="s['门店编号']">
              {{ s['门店名称'] }}（{{ s['时区'] }}）
            </option>
          </select>
        </label>
        <label class="filter-item">
          <span>营业日</span>
          <input v-model="filters.biz_day" type="date" />
        </label>
        <label class="filter-item">
          <span>或查询时点（门店本地时间）</span>
          <input v-model="filters.point" type="datetime-local" step="1" />
        </label>
        <label class="filter-item">
          <span>视角</span>
          <select v-model="filters.view">
            <option value="as_at">复盘视角（按系统收到时刻）</option>
            <option value="current">实际视角（按业务时点）</option>
          </select>
        </label>
        <button class="btn primary" type="submit">查询</button>
        <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
      </form>

      <table class="data-table">
        <thead>
          <tr>
            <th>门店</th>
            <th>时区</th>
            <th>商品</th>
            <th>营业日</th>
            <th>可用库存</th>
            <th>缺记录</th>
            <th>来源区间数</th>
            <th>来源区间</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in martRows" :key="`${row['门店编号']}-${row['商品编号']}`">
            <td>{{ row['门店名称'] }}（{{ row['门店编号'] }}）</td>
            <td>{{ row['时区'] }}</td>
            <td>{{ row['商品名称'] ?? row['商品编号'] }}</td>
            <td>{{ row['营业日'] }}</td>
            <td><strong>{{ row['可用库存'] }}</strong></td>
            <td>
              <span :class="row['缺记录商品数'] ? 'error-text' : ''">
                {{ row['缺记录商品数'] ? '是' : '否' }}
              </span>
            </td>
            <td>{{ row['来源区间数'] }}</td>
            <td>
              <button class="link" type="button" @click="toggleSources(row)">
                {{ expandedKey === rowKey(row) ? '收起区间' : '查看来源区间' }}
              </button>
            </td>
          </tr>
          <tr v-if="expandedKey">
            <td colspan="8">
              <table class="data-table sub-table">
                <thead>
                  <tr>
                    <th>调整编号</th>
                    <th>调整类型</th>
                    <th>调整数量</th>
                    <th>业务时点</th>
                    <th>系统收到时刻</th>
                    <th>业务营业日</th>
                    <th>迟到补录</th>
                    <th>批次编号</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="src in expandedSources" :key="src['调整编号']">
                    <td>{{ src['调整编号'] }}</td>
                    <td>{{ src['调整类型'] }}</td>
                    <td>{{ src['调整数量'] }}</td>
                    <td>{{ src['业务时点'] }}</td>
                    <td>{{ src['系统收到时刻'] }}</td>
                    <td>{{ src['业务营业日'] }}</td>
                    <td>{{ src['迟到补录'] ? '是' : '否' }}</td>
                    <td>{{ src['批次编号'] || '—' }}</td>
                  </tr>
                  <tr v-if="!expandedSources.length">
                    <td colspan="8" class="empty-state">该口径下没有任何有效调整来源（缺记录）</td>
                  </tr>
                </tbody>
              </table>
            </td>
          </tr>
          <tr v-if="!martRows.length">
            <td colspan="8" class="empty-state">暂无数据，请先选择营业日或查询时点</td>
          </tr>
        </tbody>
      </table>

      <footer class="page-foot">
        <span>共 {{ total }} 条，第 {{ page }} / {{ totalPages }} 页</span>
        <span>
          <button class="btn" type="button" :disabled="page <= 1" @click="reloadMart(page - 1)">上一页</button>
          <button class="btn" type="button" :disabled="page >= totalPages" @click="reloadMart(page + 1)">下一页</button>
        </span>
      </footer>

      <div class="locate-panel">
        <h3>末页回查：定位指定商品的位置与汇总贡献</h3>
        <form class="filter-bar" @submit.prevent="locateProduct">
          <label class="filter-item">
            <span>门店编号</span>
            <input v-model="locateForm.store_id" placeholder="如 ST-SH01" required />
          </label>
          <label class="filter-item">
            <span>商品编号</span>
            <input v-model="locateForm.sku" placeholder="如 SKU-1003" required />
          </label>
          <button class="btn primary" type="submit">回查位置</button>
        </form>
        <table v-if="locateResult" class="data-table">
          <tbody>
            <tr><td>全局序号</td><td>{{ locateResult['全局序号'] }}</td></tr>
            <tr><td>页号 / 页内位置</td><td>第 {{ locateResult['页号'] }} 页，第 {{ locateResult['页内位置'] }} 条</td></tr>
            <tr><td>可用库存与贡献</td><td>{{ locateResult['可用库存'] }}（库存增量 {{ locateResult['汇总贡献']['可用库存增量'] }}）</td></tr>
            <tr><td>缺记录与贡献</td><td>{{ locateResult['缺记录商品数'] ? '是' : '否' }}（缺记录增量 {{ locateResult['汇总贡献']['缺记录增量'] }}）</td></tr>
            <tr><td>来源区间数</td><td>{{ locateResult['来源区间数'] }}</td></tr>
          </tbody>
        </table>
      </div>
    </div>

    <!--  Tab 2：调整与盘点批次  -->
    <div v-if="activeTab === 'records'">
      <h3>库存调整（带业务时点、系统收到时刻与有效区间）</h3>
      <table class="data-table">
        <thead>
          <tr>
            <th>调整编号</th><th>门店</th><th>商品</th><th>类型</th><th>数量</th>
            <th>业务时点</th><th>系统收到时刻</th><th>有效起</th><th>有效止</th>
            <th>业务营业日</th><th>迟到补录</th><th>批次编号</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="adj in adjustments" :key="adj['调整编号']">
            <td>{{ adj['调整编号'] }}</td>
            <td>{{ adj['门店编号'] }}</td>
            <td>{{ adj['商品编号'] }}</td>
            <td>{{ adj['调整类型'] }}</td>
            <td>{{ adj['调整数量'] }}</td>
            <td>{{ adj['业务时点'] }}</td>
            <td>{{ adj['系统收到时刻'] }}</td>
            <td>{{ adj['有效起'] }}</td>
            <td>{{ adj['有效止'] }}</td>
            <td>{{ adj['业务营业日'] }}</td>
            <td>{{ adj['迟到补录'] ? '是' : '否' }}</td>
            <td>{{ adj['批次编号'] || '—' }}</td>
          </tr>
        </tbody>
      </table>

      <h3 style="margin-top: 18px">盘点批次（重复提交幂等，只产生一次调整）</h3>
      <table class="data-table">
        <thead>
          <tr>
            <th>批次编号</th><th>门店</th><th>商品</th><th>业务时点</th><th>系统收到时刻</th>
            <th>盘点数量</th><th>账面数量</th><th>调整数量</th><th>迟到补录</th><th>提交状态</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="batch in batches" :key="batch['批次编号']">
            <td>{{ batch['批次编号'] }}</td>
            <td>{{ batch['门店编号'] }}</td>
            <td>{{ batch['商品编号'] }}</td>
            <td>{{ batch['业务时点'] }}</td>
            <td>{{ batch['系统收到时刻'] }}</td>
            <td>{{ batch['盘点数量'] }}</td>
            <td>{{ batch['账面数量'] }}</td>
            <td>{{ batch['调整数量'] }}</td>
            <td>{{ batch['迟到补录'] ? '是' : '否' }}</td>
            <td>{{ batch['提交状态'] }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <!--  Tab 3：物化有效区间  -->
    <div v-if="activeTab === 'intervals'">
      <form class="filter-bar" @submit.prevent="loadIntervals">
        <label class="filter-item">
          <span>门店</span>
          <select v-model="intervalForm.store_id">
            <option v-for="s in stores" :key="s['门店编号']" :value="s['门店编号']">
              {{ s['门店名称'] }}（{{ s['时区'] }}）
            </option>
          </select>
        </label>
        <label class="filter-item">
          <span>商品编号</span>
          <input v-model="intervalForm.sku" placeholder="如 SKU-1001" required />
        </label>
        <button class="btn primary" type="submit">查看有效区间</button>
      </form>
      <table class="data-table">
        <thead>
          <tr>
            <th>调整编号</th><th>营业日</th><th>区间起（本地）</th><th>区间止（本地）</th>
            <th>开放区间</th><th>调整类型</th><th>数量</th><th>迟到补录</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(p, i) in intervalPieces" :key="`${p['调整编号']}-${i}`">
            <td>{{ p['调整编号'] }}</td>
            <td>{{ p['营业日'] }}</td>
            <td>{{ p['区间起'] }}</td>
            <td>{{ p['区间止'] }}</td>
            <td>{{ p['开放区间'] ? '是' : '否' }}</td>
            <td>{{ p['调整类型'] }}</td>
            <td>{{ p['调整数量'] }}</td>
            <td>{{ p['迟到补录'] ? '是' : '否' }}</td>
          </tr>
          <tr v-if="!intervalPieces.length">
            <td colspan="8" class="empty-state">请选择门店与商品后查询</td>
          </tr>
        </tbody>
      </table>
      <p class="page-desc">跨午夜调整按门店营业日界拆成半开子区间，相邻区间端点不重叠，同一调整编号不会被重复计入。</p>
    </div>

    <footer class="page-foot">
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
      <span v-else-if="okMessage" style="color: #067647">{{ okMessage }}</span>
    </footer>

    <Teleport to="body">
      <div v-if="showBatchModal" class="modal-overlay" @click.self="showBatchModal = false">
        <div class="modal-card">
          <header class="modal-head">
            <h3>登记盘点批次</h3>
            <button class="btn ghost" type="button" @click="showBatchModal = false">&times;</button>
          </header>
          <form class="modal-body" @submit.prevent="submitBatch">
            <label v-for="field in batchFields" :key="field.key" class="modal-field">
              <span>{{ field.label }}</span>
              <input v-model="batchForm[field.key]" :type="field.type ?? 'text'" :placeholder="field.placeholder" required />
            </label>
            <footer class="modal-foot">
              <button class="btn" type="button" @click="showBatchModal = false">取消</button>
              <button class="btn primary" type="submit">提交批次</button>
            </footer>
          </form>
        </div>
      </div>
    </Teleport>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'

import { request } from '@/api/client'

const ENDPOINT = '/api/inventory_timeline'

type Row = Record<string, any>

const tabs = [
  { key: 'mart', label: '库存时点集市' },
  { key: 'records', label: '调整与盘点批次' },
  { key: 'intervals', label: '有效区间' },
]
const activeTab = ref('mart')

const stores = ref<Row[]>([])
const martRows = ref<Row[]>([])
const total = ref(0)
const page = ref(1)
const size = 20
const summary = ref<Record<string, number>>({})
const errorMessage = ref('')
const okMessage = ref('')
const expandedKey = ref('')
const expandedSources = ref<Row[]>([])

const filters = reactive({
  store_id: '',
  biz_day: '2026-11-17',
  point: '',
  view: 'as_at',
})

const locateForm = reactive({ store_id: 'ST-SH01', sku: 'SKU-1003' })
const locateResult = ref<Row | null>(null)

const adjustments = ref<Row[]>([])
const batches = ref<Row[]>([])
const intervalForm = reactive({ store_id: 'ST-SH01', sku: 'SKU-1001' })
const intervalPieces = ref<Row[]>([])

const showBatchModal = ref(false)
const batchFields = [
  { key: '批次编号', label: '批次编号', placeholder: '如 BATCH-C03' },
  { key: '门店编号', label: '门店编号', placeholder: '如 ST-SH01' },
  { key: '商品编号', label: '商品编号', placeholder: '如 SKU-1002' },
  { key: '盘点数量', label: '盘点数量', type: 'number', placeholder: '整数' },
  { key: '业务时点', label: '业务时点', placeholder: '2026-11-18 10:00:00' },
  { key: '系统收到时刻', label: '系统收到时刻', placeholder: '2026-11-18 10:10:00' },
]
const batchForm = reactive<Record<string, string | number>>({})

const totalPages = computed(() => Math.max(1, Math.ceil(total.value / size)))

function rowKey(row: Row): string {
  return `${row['门店编号']}-${row['商品编号']}`
}

function toggleSources(row: Row) {
  if (expandedKey.value === rowKey(row)) {
    expandedKey.value = ''
    expandedSources.value = []
    return
  }
  expandedKey.value = rowKey(row)
  expandedSources.value = row['来源区间'] ?? []
}

async function getJson(path: string): Promise<any> {
  const response = await request(`${ENDPOINT}${path}`)
  if (!response.ok) {
    const data = await response.json().catch(() => ({}))
    throw new Error(data.detail ?? `接口返回 ${response.status}`)
  }
  return response.json()
}

async function loadStores() {
  const data = await getJson('/stores')
  stores.value = data.items ?? []
}

async function reloadMart(targetPage = 1) {
  errorMessage.value = ''
  page.value = targetPage
  const params = new URLSearchParams({ page: String(page.value), size: String(size), view: filters.view })
  if (filters.store_id) params.set('store_id', filters.store_id)
  if (filters.point) params.set('point', filters.point)
  if (filters.biz_day && !filters.point) params.set('biz_day', filters.biz_day)
  try {
    const data = await getJson(`/mart?${params.toString()}`)
    martRows.value = data.items ?? []
    total.value = data.total ?? 0
    summary.value = data['汇总'] ?? {}
    expandedKey.value = ''
    expandedSources.value = []
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '集市查询失败'
  }
}

function resetFilters() {
  filters.store_id = ''
  filters.biz_day = '2026-11-17'
  filters.point = ''
  filters.view = 'as_at'
  void reloadMart(1)
}

async function locateProduct() {
  errorMessage.value = ''
  const params = new URLSearchParams({
    store_id: locateForm.store_id,
    sku: locateForm.sku,
    view: filters.view,
    size: String(size),
  })
  if (filters.point) params.set('point', filters.point)
  if (filters.biz_day && !filters.point) params.set('biz_day', filters.biz_day)
  try {
    locateResult.value = await getJson(`/locate?${params.toString()}`)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '回查失败'
    locateResult.value = null
  }
}

async function loadRecords() {
  const [adjData, batchData] = await Promise.all([getJson('/adjustments'), getJson('/batches')])
  adjustments.value = adjData.items ?? []
  batches.value = batchData.items ?? []
}

async function loadIntervals() {
  errorMessage.value = ''
  try {
    const data = await getJson(
      `/intervals?store_id=${encodeURIComponent(intervalForm.store_id)}&sku=${encodeURIComponent(intervalForm.sku)}`,
    )
    intervalPieces.value = data.items ?? []
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '有效区间查询失败'
  }
}

function switchTab(key: string) {
  activeTab.value = key
  errorMessage.value = ''
  okMessage.value = ''
  if (key === 'records') void loadRecords()
  if (key === 'intervals') void loadIntervals()
}

function openBatchModal() {
  errorMessage.value = ''
  okMessage.value = ''
  Object.keys(batchForm).forEach((k) => delete batchForm[k])
  showBatchModal.value = true
}

async function submitBatch() {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/count_batches`, {
      method: 'POST',
      body: JSON.stringify({ values: { ...batchForm } }),
    })
    const data = await response.json()
    if (!response.ok || !data.ok) {
      throw new Error(data.message || '批次提交失败')
    }
    okMessage.value = data.message
    showBatchModal.value = false
    await Promise.all([loadRecords(), reloadMart(page.value)])
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '批次提交失败'
  }
}

onMounted(async () => {
  await loadStores()
  await reloadMart(1)
})
</script>

<style scoped>
.tab-bar {
  display: flex;
  gap: 8px;
  margin-bottom: 12px;
}
.sub-table {
  background: #f8fafc;
}
.sub-table th,
.sub-table td {
  font-size: 12px;
  padding: 6px 8px;
}
.locate-panel {
  margin-top: 18px;
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 14px 16px;
}
.locate-panel h3 {
  margin: 0 0 10px;
  font-size: 15px;
}
.page-actions {
  display: flex;
  gap: 8px;
}
</style>
