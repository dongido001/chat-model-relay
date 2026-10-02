<template>
  <div class="space-y-6">
    <!-- Top Action Bar -->
    <div class="glass-panel-elevated rounded-2xl p-5 sm:p-6 flex flex-col md:flex-row md:items-center justify-between gap-4">
      <div class="space-y-1">
        <div class="flex items-center space-x-2">
          <Settings class="w-5 h-5 text-indigo-400" />
          <h2 class="text-base font-bold text-white tracking-tight">Relay Configuration & Environment</h2>
          <span
            v-if="hasChanges"
            class="px-2 py-0.5 rounded-full text-[10px] font-bold bg-amber-500/20 text-amber-300 border border-amber-500/30 animate-pulse"
          >
            {{ changeCount }} unsaved change{{ changeCount === 1 ? '' : 's' }}
          </span>
        </div>
        <p class="text-xs text-slate-400 flex items-center space-x-2">
          <span>Target File:</span>
          <code class="px-2 py-0.5 rounded bg-slate-900 border border-white/10 text-cyan-300 font-mono text-[11px] truncate max-w-sm sm:max-w-md">
            {{ store.envPath || '.env' }}
          </code>
        </p>
      </div>

      <!-- Action Buttons -->
      <div class="flex items-center space-x-3 shrink-0">
        <button
          v-if="hasChanges"
          @click="resetChanges"
          class="px-3.5 py-2 rounded-xl bg-slate-800 hover:bg-slate-700 text-slate-300 text-xs font-semibold border border-white/10 transition-all cursor-pointer hover:scale-105 active:scale-95"
        >
          Discard
        </button>

        <button
          @click="handleSave"
          :disabled="!hasChanges || isSaving"
          class="px-4 py-2 rounded-xl bg-indigo-600 hover:bg-indigo-500 disabled:opacity-40 disabled:hover:scale-100 disabled:cursor-not-allowed text-white text-xs font-semibold shadow-md shadow-indigo-600/30 transition-all cursor-pointer flex items-center space-x-1.5 hover:scale-105 active:scale-95"
        >
          <Save class="w-3.5 h-3.5" />
          <span>{{ isSaving ? "Saving..." : "Save Settings" }}</span>
        </button>

        <button
          @click="showRestartModal = true"
          class="px-3.5 py-2 rounded-xl bg-slate-900 hover:bg-rose-950/60 hover:text-rose-300 text-slate-400 text-xs font-semibold border border-white/10 hover:border-rose-500/30 transition-all cursor-pointer flex items-center space-x-1.5 hover:scale-105 active:scale-95"
          title="Restart Relay Process"
        >
          <RotateCcw class="w-3.5 h-3.5" />
          <span>Restart Relay</span>
        </button>
      </div>
    </div>

    <!-- Filter & Category Tabs -->
    <div class="flex flex-col sm:flex-row items-stretch sm:items-center justify-between gap-3">
      <div class="flex items-center space-x-1 overflow-x-auto pb-1 sm:pb-0 scrollbar-none">
        <button
          v-for="cat in categories"
          :key="cat"
          @click="selectedCategory = cat"
          :class="[
            selectedCategory === cat
              ? 'bg-indigo-600 text-white shadow-sm shadow-indigo-500/25'
              : 'bg-slate-900/80 text-slate-400 hover:text-slate-200 hover:bg-white/5',
            'px-3 py-1.5 rounded-lg text-xs font-medium whitespace-nowrap border border-white/5 transition-all cursor-pointer hover:scale-[1.02] active:scale-[0.98]'
          ]"
        >
          {{ cat }}
        </button>
      </div>

      <div class="relative w-full sm:w-64">
        <Search class="w-3.5 h-3.5 absolute left-3 top-1/2 -translate-y-1/2 text-slate-500" />
        <input
          v-model="searchQuery"
          type="text"
          placeholder="Search setting key or desc..."
          class="w-full bg-slate-900/90 border border-white/10 rounded-xl pl-8 pr-3 py-1.5 text-xs text-slate-200 placeholder:text-slate-600 focus:border-indigo-500 outline-none transition-colors"
        />
      </div>
    </div>

    <!-- Settings Grid -->
    <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
      <div
        v-for="item in filteredSettings"
        :key="item.name"
        class="glass-panel rounded-2xl p-5 flex flex-col justify-between hover:border-indigo-500/30 transition-all duration-200"
      >
        <div>
          <!-- Header: Setting name + badges -->
          <div class="flex items-start justify-between gap-2 mb-1.5">
            <span class="font-mono text-xs font-bold text-slate-200 tracking-wide select-all">
              {{ item.name }}
            </span>
            <div class="flex items-center space-x-1.5 shrink-0">
              <span class="px-2 py-0.5 rounded text-[10px] font-semibold bg-slate-800 text-slate-400 border border-white/5">
                {{ item.category }}
              </span>
              <span
                v-if="item.restart_required"
                class="px-1.5 py-0.5 rounded text-[9px] font-medium bg-amber-500/10 text-amber-400 border border-amber-500/20"
                title="Restarting the relay is required for this setting to take effect"
              >
                Restart req.
              </span>
            </div>
          </div>

          <!-- Description -->
          <p class="text-xs text-slate-400 mb-4 leading-relaxed">
            {{ item.description }}
          </p>
        </div>

        <!-- Input Control -->
        <div class="pt-3 border-t border-white/5">
          <!-- Boolean Toggle -->
          <div v-if="item.type === 'bool'" class="flex items-center justify-between">
            <span class="text-xs text-slate-400 font-mono">
              {{ getCurrentValue(item.name) === true || String(getCurrentValue(item.name)).toLowerCase() === 'true' ? 'Enabled' : 'Disabled' }}
            </span>
            <button
              type="button"
              @click="toggleBool(item.name)"
              :class="[
                (getCurrentValue(item.name) === true || String(getCurrentValue(item.name)).toLowerCase() === 'true')
                  ? 'bg-emerald-500 shadow-sm shadow-emerald-500/30'
                  : 'bg-slate-800',
                'relative inline-flex h-6 w-11 shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors duration-200 ease-in-out focus:outline-none hover:scale-105 active:scale-95'
              ]"
            >
              <span
                :class="[
                  (getCurrentValue(item.name) === true || String(getCurrentValue(item.name)).toLowerCase() === 'true')
                    ? 'translate-x-5'
                    : 'translate-x-0',
                  'pointer-events-none inline-block h-5 w-5 transform rounded-full bg-white shadow ring-0 transition duration-200 ease-in-out'
                ]"
              />
            </button>
          </div>

          <!-- Choice Select -->
          <div v-else-if="item.type === 'choice'" class="relative">
            <select
              :value="getCurrentValue(item.name)"
              @change="updateValue(item.name, ($event.target as HTMLSelectElement).value)"
              class="w-full bg-slate-900 border border-white/10 rounded-xl px-3 py-2 text-xs font-mono text-indigo-300 outline-none focus:border-indigo-500 transition-colors cursor-pointer"
            >
              <option v-for="c in item.choices || []" :key="c" :value="c">
                {{ c }}
              </option>
            </select>
          </div>

          <!-- Integer Input -->
          <div v-else-if="item.type === 'int'" class="space-y-1">
            <div class="flex items-center space-x-2">
              <input
                type="number"
                :min="item.min !== null && item.min !== undefined ? item.min : undefined"
                :max="item.max !== null && item.max !== undefined ? item.max : undefined"
                :value="getCurrentValue(item.name)"
                @input="updateValue(item.name, Number(($event.target as HTMLInputElement).value))"
                class="w-full bg-slate-900 border border-white/10 rounded-xl px-3 py-1.5 text-xs font-mono text-cyan-300 outline-none focus:border-indigo-500 transition-colors"
              />
            </div>
            <div v-if="item.min !== null || item.max !== null" class="text-[10px] text-slate-500 text-right font-mono">
              Range: {{ item.min ?? 0 }} - {{ item.max ?? 'unlimited' }}
            </div>
          </div>

          <!-- Text Input -->
          <div v-else>
            <input
              type="text"
              :value="getCurrentValue(item.name) ?? ''"
              @input="updateValue(item.name, ($event.target as HTMLInputElement).value)"
              placeholder="Value not set (default)"
              class="w-full bg-slate-900 border border-white/10 rounded-xl px-3 py-1.5 text-xs font-mono text-slate-200 placeholder:text-slate-600 outline-none focus:border-indigo-500 transition-colors"
            />
          </div>
        </div>
      </div>
    </div>

    <!-- Empty State -->
    <div v-if="filteredSettings.length === 0" class="glass-panel rounded-2xl py-16 px-6 text-center">
      <Settings class="w-8 h-8 text-slate-600 mx-auto mb-3" />
      <p class="text-sm font-semibold text-slate-300">No settings match your filter</p>
      <p class="text-xs text-slate-500 mt-1">Try searching for a different keyword or selecting "All Categories".</p>
    </div>

    <!-- Confirmation Modal for Restart -->
    <div
      v-if="showRestartModal"
      class="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/60 backdrop-blur-sm"
    >
      <div class="glass-panel-elevated max-w-md w-full rounded-2xl p-6 space-y-4 shadow-2xl border border-white/10">
        <div class="flex items-center space-x-3 text-amber-400">
          <RotateCcw class="w-6 h-6 shrink-0" />
          <h3 class="text-base font-bold text-white tracking-tight">Restart Chat Model Relay?</h3>
        </div>
        <p class="text-xs text-slate-300 leading-relaxed">
          The relay process will immediately restart. Any in-flight chat completion requests will be aborted, and Playwright browser instances will reinitialize.
        </p>
        <div class="flex items-center justify-end space-x-3 pt-2">
          <button
            @click="showRestartModal = false"
            class="px-4 py-2 rounded-xl bg-slate-800 hover:bg-slate-700 text-slate-300 text-xs font-semibold transition-all cursor-pointer hover:scale-105 active:scale-95"
          >
            Cancel
          </button>
          <button
            @click="confirmRestart"
            class="px-4 py-2 rounded-xl bg-rose-600 hover:bg-rose-500 text-white text-xs font-semibold shadow-md shadow-rose-600/30 transition-all cursor-pointer hover:scale-105 active:scale-95"
          >
            Confirm Restart
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed } from "vue"
import { useRelayStore } from "../../src/stores/relay"
import { Settings, Save, RotateCcw, Search } from "lucide-vue-next"

const store = useRelayStore()
const searchQuery = ref("")
const selectedCategory = ref("All")
const isSaving = ref(false)
const showRestartModal = ref(false)

const localEdits = ref<Record<string, any>>({})

const categories = computed(() => {
  const set = new Set<string>()
  set.add("All")
  for (const item of store.settings) {
    if (item.category) set.add(item.category)
  }
  return Array.from(set)
})

const changeCount = computed(() => Object.keys(localEdits.value).length)
const hasChanges = computed(() => changeCount.value > 0)

function getCurrentValue(name: string) {
  if (name in localEdits.value) {
    return localEdits.value[name]
  }
  const item = store.settings.find(s => s.name === name)
  return item ? item.value : ""
}

function updateValue(name: string, val: any) {
  localEdits.value[name] = val
}

function toggleBool(name: string) {
  const current = getCurrentValue(name)
  const isCurrentlyTrue = current === true || String(current).toLowerCase() === "true"
  localEdits.value[name] = !isCurrentlyTrue
}

function resetChanges() {
  localEdits.value = {}
  store.showToast("Local edits discarded", "info")
}

async function handleSave() {
  isSaving.value = true
  const success = await store.saveSettings(localEdits.value)
  if (success) {
    localEdits.value = {}
  }
  isSaving.value = false
}

async function confirmRestart() {
  showRestartModal.value = false
  await store.restartService()
}

const filteredSettings = computed(() => {
  return store.settings.filter(item => {
    if (selectedCategory.value !== "All" && item.category !== selectedCategory.value) {
      return false
    }
    if (searchQuery.value.trim()) {
      const q = searchQuery.value.toLowerCase()
      const matchName = item.name.toLowerCase().includes(q)
      const matchDesc = (item.description || "").toLowerCase().includes(q)
      if (!matchName && !matchDesc) return false
    }
    return true
  })
})
</script>
