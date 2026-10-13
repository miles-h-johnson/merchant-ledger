import { useCallback, useEffect, useMemo, useState } from 'react'

const SETTLEMENT_OPTIONS = [
  { label: 'Current Price', value: 'current' },
  { label: 'Industrial Town', value: 'industrial_town' },
  { label: 'Industrial City', value: 'industrial_city' },
  { label: 'Market Town', value: 'market_town' },
  { label: 'Market City', value: 'market_city' },
  { label: 'Religious Town', value: 'religious_town' },
  { label: 'Temple City', value: 'temple_city' },
]

const SOURCE_STYLES = {
  lr_price: {
    label: 'Empire Rate ✓',
    classes: 'bg-sky-950/45 text-sky-200 border border-sky-700/70',
  },
  manual_override: {
    label: 'Set Price',
    classes: 'bg-purple-900/40 text-purple-300 border border-purple-700/70',
  },
  recipe: {
    label: 'Recipe',
    classes: 'bg-amber-900/40 text-amber-300 border border-amber-700/70',
  },
  unresolved: {
    label: 'Unknown',
    classes: 'bg-red-900/40 text-red-300 border border-red-700/70',
  },
  not_found: {
    label: 'Unknown',
    classes: 'bg-red-900/40 text-red-300 border border-red-700/70',
  },
}

const QUALITY_LABELS = ['common', 'uncommon', 'rare', 'epic', 'legendary']
const MAX_RECIPE_TREE_DEPTH = 2
const LABOR_MARKUP_STORAGE_KEY = 'vs-labor-markup-enabled'

function formatCost(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) {
    return '—'
  }
  return Number(value).toLocaleString(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })
}

function getUnresolvedReason(source) {
  if (source === 'not_found') return 'These runes are unknown to Bomrek.'
  return 'The runes do not glow.'
}

function formatCompleteness(item) {
  const resolved = Number(item?.resolved_ingredient_count)
  const total = Number(item?.total_ingredient_count)

  if (!Number.isFinite(resolved) || !Number.isFinite(total) || total <= 0) {
    return null
  }

  const pct = Math.round((resolved / total) * 100)
  return `${resolved}/${total} ingredients priced (${pct}%)`
}

function cleanDisplayName(name) {
  return String(name || '')
    .replace(/\s*\([^)]*\)\s*$/g, '')
    .trim()
}

function isVariantFamilySuggestion(suggestion) {
  return (
    !!suggestion?.variant_family &&
    Array.isArray(suggestion?.available_materials) &&
    suggestion.available_materials.length > 0
  )
}

function formatCategoryLabel(category) {
  const normalized = String(category || '').trim()
  if (!normalized) return ''

  return normalized
    .replace(/_/g, ' ')
    .toLowerCase()
    .replace(/\b\w/g, (char) => char.toUpperCase())
}

function collectUnresolvedItems(items) {
  const unresolvedMap = new Map()

  const walk = (nodes) => {
    if (!Array.isArray(nodes) || !nodes.length) return

    nodes.forEach((node) => {
      if (!node || typeof node !== 'object') return

      if (node.source === 'unresolved' || node.source === 'not_found') {
        const name = node.display_name || node.raw || node.canonical_id || 'Unknown item'
        const reason = getUnresolvedReason(node.source)
        const key = name
        const existing = unresolvedMap.get(key)

        if (existing) {
          existing.count += 1
        } else {
          unresolvedMap.set(key, {
            name,
            reason,
            count: 1,
          })
        }
      }

      if (Array.isArray(node.ingredients) && node.ingredients.length) {
        walk(node.ingredients)
      }
    })
  }

  walk(items)
  return Array.from(unresolvedMap.values())
}

function stripZipSuffix(modName) {
  return String(modName || '').replace(/\.zip(?:_.+)?$/i, '').trim()
}

function getQualityValue(priceMap, tier) {
  const value = Number(priceMap?.[tier])
  return Number.isFinite(value) ? value : null
}

function getAvailableQualityTiers(priceMap) {
  return QUALITY_LABELS.filter((tier) => getQualityValue(priceMap, tier) !== null)
}

function App() {
  const [order, setOrder] = useState('')
  const [settlementType, setSettlementType] = useState('current')
  const [laborMarkupEnabled, setLaborMarkupEnabled] = useState(() => {
    try {
      return window.localStorage.getItem(LABOR_MARKUP_STORAGE_KEY) === 'true'
    } catch {
      return false
    }
  })
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [result, setResult] = useState(null)
  const [missingMods, setMissingMods] = useState([])
  const [diagOpen, setDiagOpen] = useState(true)
  const [expandedRecipeNodes, setExpandedRecipeNodes] = useState({})
  const [qualityAllocations, setQualityAllocations] = useState({})

  const [suggestions, setSuggestions] = useState([])
  const [activeSuggestion, setActiveSuggestion] = useState(0)

  useEffect(() => {
    try {
      window.localStorage.setItem(LABOR_MARKUP_STORAGE_KEY, laborMarkupEnabled ? 'true' : 'false')
    } catch {
      // ignore storage failures
    }
  }, [laborMarkupEnabled])

  useEffect(() => {
    let cancelled = false

    const fetchMissingMods = async () => {
      try {
        const response = await fetch('/diagnostics/missing-mods')
        if (!response.ok) {
          if (!cancelled) setMissingMods([])
          return
        }

        const data = await response.json()
        if (!cancelled) {
          setMissingMods(Array.isArray(data) ? data : [])
        }
      } catch {
        if (!cancelled) setMissingMods([])
      }
    }

    fetchMissingMods()

    return () => {
      cancelled = true
    }
  }, [])

  useEffect(() => {
    setExpandedRecipeNodes({})
    const nextAllocations = {}

    ;(result?.items || []).forEach((item, index) => {
      const tiers = getAvailableQualityTiers(item?.quality_prices)
      if (!tiers.length) return

      const quantity = Math.max(0, Number(item?.quantity) || 0)
      const allocation = Object.fromEntries(tiers.map((tier) => [tier, 0]))

      if (tiers.includes('common')) {
        allocation.common = quantity
      } else {
        allocation[tiers[0]] = quantity
      }

      nextAllocations[index] = allocation
    })

    setQualityAllocations(nextAllocations)
  }, [result])

  const handleQualityAllocationChange = useCallback((itemIndex, tier, rawValue) => {
    const numeric = Number(rawValue)
    const nextValue = Number.isFinite(numeric) ? Math.max(0, numeric) : 0

    setQualityAllocations((prev) => ({
      ...prev,
      [itemIndex]: {
        ...(prev[itemIndex] || {}),
        [tier]: nextValue,
      },
    }))
  }, [])

  const displayedItems = useMemo(() => {
    return (result?.items || []).map((item, index) => {
      const tiers = getAvailableQualityTiers(item?.quality_prices)
      if (!tiers.length) {
        return {
          index,
          item,
          unitCost: item?.unit_cost,
          totalCost: item?.total_cost,
          qualityBreakdown: null,
        }
      }

      const quantity = Math.max(0, Number(item?.quantity) || 0)
      const allocation = qualityAllocations[index] || {}

      let allocatedTotal = 0
      let adjustedTotal = 0

      tiers.forEach((tier) => {
        const qty = Math.max(0, Number(allocation[tier]) || 0)
        const tierPrice = getQualityValue(item?.quality_prices, tier) || 0
        allocatedTotal += qty
        adjustedTotal += qty * tierPrice
      })

      const adjustedUnit = quantity > 0 ? adjustedTotal / quantity : 0
      const isAllocationValid = Math.abs(allocatedTotal - quantity) < 1e-9

      return {
        index,
        item,
        unitCost: adjustedUnit,
        totalCost: adjustedTotal,
        qualityBreakdown: {
          tiers,
          allocation,
          allocatedTotal,
          quantity,
          isAllocationValid,
        },
      }
    })
  }, [qualityAllocations, result])

  const displayedTotals = useMemo(() => {
    const fallbackTotals = result?.totals || {}

    const computed = displayedItems.reduce(
      (acc, entry) => {
        const totalCostValue = Number(entry?.totalCost)
        if (!Number.isFinite(totalCostValue)) {
          return acc
        }

        acc.totalCombined += totalCostValue

        if (
          entry?.item?.source === 'lr_price' ||
          entry?.item?.source === 'manual_override'
        ) {
          acc.totalLR += totalCostValue
        } else if (entry?.item?.source === 'recipe') {
          acc.totalRecipe += totalCostValue
        }

        return acc
      },
      {
        totalLR: 0,
        totalRecipe: 0,
        totalCombined: 0,
      },
    )

    return {
      totalLRCost: displayedItems.length ? computed.totalLR : Number(fallbackTotals.total_lr_cost || 0),
      totalRecipeCost: displayedItems.length ? computed.totalRecipe : Number(fallbackTotals.total_recipe_cost || 0),
      totalCombined: displayedItems.length ? computed.totalCombined : Number(fallbackTotals.total_combined || 0),
      hasUnresolved: !!fallbackTotals.has_unresolved,
    }
  }, [displayedItems, result])

  const unresolvedItems = useMemo(() => collectUnresolvedItems(result?.items || []), [result])

  const getActiveSegment = useCallback((rawText) => {
    const text = String(rawText || '')
    const lastComma = text.lastIndexOf(',')
    const segmentStart = lastComma >= 0 ? lastComma + 1 : 0
    const before = text.slice(0, segmentStart)
    const segment = text.slice(segmentStart)

    const qtyMatch = segment.match(/^(\s*\d+(?:\.\d+)?\s+)(.*)$/)
    const qtyPrefix = qtyMatch ? qtyMatch[1] : ''
    const query = (qtyMatch ? qtyMatch[2] : segment).trim()

    return { before, qtyPrefix, query }
  }, [])

  const applySuggestion = useCallback(
    (itemName) => {
      if (!itemName) return

      const { before, qtyPrefix } = getActiveSegment(order)
      const rebuiltSegment = qtyPrefix ? `${qtyPrefix} ${itemName}`.trim() : itemName

      const previousPrefix = before.trimEnd()
      const nextOrder = previousPrefix
        ? `${previousPrefix} ${rebuiltSegment}`
        : rebuiltSegment

      setOrder(nextOrder)
      setSuggestions([])
      setActiveSuggestion(0)
    },
    [getActiveSegment, order],
  )

  const applyMaterialSuggestion = useCallback(
    (material, familyName) => {
      const familyLabel = cleanDisplayName(familyName)
      const normalized = `${String(material || '').trim()} ${familyLabel}`.trim()
      if (!normalized) return
      applySuggestion(normalized)
    },
    [applySuggestion],
  )

  const fetchSuggestions = useCallback(async () => {
    const { query } = getActiveSegment(order)
    if (query.length < 2) {
      setSuggestions([])
      return
    }

    try {
      const response = await fetch(`/search?q=${encodeURIComponent(query)}&limit=10`)
      if (!response.ok) {
        setSuggestions([])
        return
      }

      const data = await response.json()
      setSuggestions(Array.isArray(data.results) ? data.results : [])
      setActiveSuggestion(0)
    } catch {
      setSuggestions([])
    }
  }, [getActiveSegment, order])

  useEffect(() => {
    const timer = setTimeout(() => {
      fetchSuggestions()
    }, 300)

    return () => clearTimeout(timer)
  }, [order, fetchSuggestions])

  const handleCalculate = useCallback(async () => {
    if (!order.trim()) {
      setError('Speak your commission, traveller.')
      setResult(null)
      return
    }

    setLoading(true)
    setError('')

    try {
      const response = await fetch('/calculate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          order: order.trim(),
          settlement_type: settlementType,
          labor_markup: laborMarkupEnabled,
        }),
      })

      const data = await response.json()
      if (!response.ok) {
        throw new Error(data?.error || 'The runes do not glow.')
      }

      setResult(data)
    } catch (err) {
      const rawMessage = String(err?.message || '').trim()
      const normalized = rawMessage.toLowerCase()

      if (normalized.includes('not found') || normalized.includes('not recognized') || normalized.includes('unknown')) {
        setError('These runes are unknown to Bomrek.')
      } else {
        setError('The runes do not glow.')
      }

      setResult(null)
    } finally {
      setLoading(false)
    }
  }, [laborMarkupEnabled, order, settlementType])

  const onOrderKeyDown = useCallback(
    (event) => {
      if (!suggestions.length) return

      if (event.key === 'ArrowDown') {
        event.preventDefault()
        setActiveSuggestion((prev) => (prev + 1) % suggestions.length)
      } else if (event.key === 'ArrowUp') {
        event.preventDefault()
        setActiveSuggestion((prev) => (prev - 1 + suggestions.length) % suggestions.length)
      } else if (event.key === 'Enter' && suggestions[activeSuggestion]) {
        event.preventDefault()
        const selected = suggestions[activeSuggestion]
        if (isVariantFamilySuggestion(selected)) {
          const firstMaterial = selected.available_materials[0]
          if (firstMaterial) {
            applyMaterialSuggestion(firstMaterial, selected.display_name)
          }
        } else {
          applySuggestion(selected.display_name)
        }
      } else if (event.key === 'Escape') {
        setSuggestions([])
      }
    },
    [activeSuggestion, applyMaterialSuggestion, applySuggestion, suggestions],
  )

  const renderIngredientTree = useCallback(
    (ingredients, depth = 0, parentPath = 'root') => {
      if (!ingredients?.length) {
        return <p className="text-stone-500 italic">No ingredient details.</p>
      }

      return (
        <ul className="space-y-2">
          {ingredients.map((ingredient, index) => {
            const itemKey = `${ingredient.canonical_id || ingredient.display_name || 'ing'}-${depth}-${index}`
            const nodePath = `${parentPath}-${index}-${ingredient.canonical_id || ingredient.display_name || 'ing'}`
            const hasChildren = !!ingredient.ingredients?.length
            const isAtDepthCutoff = depth >= MAX_RECIPE_TREE_DEPTH
            const isExpanded = !!expandedRecipeNodes[nodePath]
            const canShowChildren = hasChildren && (!isAtDepthCutoff || isExpanded)
            const ingredientLabel =
              cleanDisplayName(ingredient.display_name) ||
              ingredient.canonical_id ||
              'Unknown ingredient'

            return (
              <li
                key={itemKey}
                className="rounded border border-stone-700/60 bg-zinc-900/40 p-2"
                style={{ marginLeft: depth ? `${depth * 12}px` : 0 }}
              >
                <div className="flex min-w-0 flex-wrap items-start gap-2 text-sm">
                  <span className="min-w-0 flex-1 break-words font-medium text-stone-100" style={{ overflowWrap: 'break-word' }}>
                    {ingredientLabel}
                  </span>
                  <span className="shrink-0 font-mono text-amber-200">
                    {formatCost(ingredient.quantity)} × {formatCost(ingredient.unit_cost)} = {formatCost(ingredient.total_cost)}
                  </span>
                </div>

                {hasChildren && isAtDepthCutoff && !isExpanded ? (
                  <button
                    type="button"
                    onClick={() =>
                      setExpandedRecipeNodes((prev) => ({
                        ...prev,
                        [nodePath]: true,
                      }))
                    }
                    className="mt-2 text-xs text-amber-300 hover:text-amber-200"
                  >
                    ▼ show deeper
                  </button>
                ) : null}

                {canShowChildren ? (
                  <div className="mt-2 overflow-x-hidden break-words" style={{ overflowWrap: 'break-word' }}>
                    {renderIngredientTree(ingredient.ingredients, depth + 1, nodePath)}
                  </div>
                ) : null}
              </li>
            )
          })}
        </ul>
      )
    },
    [expandedRecipeNodes],
  )

  return (
    <div
      className="runic-app min-h-screen px-4 py-8 text-stone-100"
    >
      <div
        className="runic-panel mx-auto w-full max-w-7xl rounded-2xl p-4 shadow-2xl shadow-black/70 backdrop-blur-sm sm:p-6"
      >
        <header className="mb-6 border-b border-amber-800/40 pb-4">
          <p className="text-xs uppercase tracking-[0.25em] text-amber-400">Bomrek — Runesmith of Tharagdum</p>
          <h1 className="runic-heading mt-2 text-2xl text-amber-100 sm:text-4xl">Runic Abacus</h1>
          <p className="mt-2 text-sm text-stone-300 sm:text-base">Carved in stone, priced in gold</p>
          <div className="runic-divider mt-4" aria-hidden="true" />
        </header>

        <section className="grid gap-4 lg:grid-cols-3">
          <div className="relative lg:col-span-2">
            <label className="runic-heading mb-2 block text-lg text-amber-100">Commission</label>
            <textarea
              value={order}
              onChange={(event) => setOrder(event.target.value)}
              onBlur={() => setTimeout(() => setSuggestions([]), 150)}
              onKeyDown={onOrderKeyDown}
              rows={6}
              className="stone-input w-full rounded-lg p-3 text-stone-100 outline-none transition"
              placeholder="e.g. 4 iron plates, 1 iron brigandine, 10 copper ingots"
            />

            {suggestions.length > 0 ? (
              <div className="absolute z-10 mt-1 max-h-60 w-full overflow-y-auto rounded-lg border border-amber-800/70 bg-zinc-950/95 shadow-xl">
                {suggestions.map((suggestion, index) => {
                  const isVariantFamily = isVariantFamilySuggestion(suggestion)
                  const rowClasses = `w-full px-3 py-2 text-left text-sm transition ${
                    index === activeSuggestion ? 'bg-amber-800/40 text-amber-100' : 'text-stone-200 hover:bg-zinc-800/80'
                  }`

                  if (isVariantFamily) {
                    return (
                      <div key={`${suggestion.canonical_id}-${index}`} className={rowClasses}>
                        <div className="min-w-0">
                          <div className="flex items-center justify-between gap-3">
                            <span className="truncate font-semibold">{cleanDisplayName(suggestion.display_name)}</span>
                            {suggestion.price_current !== null ? (
                              <span className="shrink-0 font-mono text-xs text-amber-300">{formatCost(suggestion.price_current)}</span>
                            ) : null}
                          </div>

                          <div className="mt-1 flex flex-wrap gap-1">
                            {suggestion.available_materials.map((material) => (
                              <button
                                type="button"
                                key={`${suggestion.canonical_id}-${material}`}
                                onMouseDown={(event) => {
                                  event.preventDefault()
                                  applyMaterialSuggestion(material, suggestion.display_name)
                                }}
                                className="rounded border border-amber-700/70 bg-amber-900/30 px-2 py-0.5 text-xs text-amber-200 hover:bg-amber-800/50"
                              >
                                {material}
                              </button>
                            ))}
                          </div>

                          {suggestion.lr_category ? (
                            <div className="mt-1 text-xs text-stone-400">{formatCategoryLabel(suggestion.lr_category)}</div>
                          ) : null}
                        </div>
                      </div>
                    )
                  }

                  return (
                    <button
                      type="button"
                      key={`${suggestion.canonical_id}-${index}`}
                      onMouseDown={(event) => {
                        event.preventDefault()
                        applySuggestion(suggestion.display_name)
                      }}
                      className={rowClasses}
                    >
                      <div className="min-w-0">
                        <div className="flex items-center justify-between gap-3">
                          <span className="truncate">{cleanDisplayName(suggestion.display_name)}</span>
                          {suggestion.price_current !== null ? (
                            <span className="shrink-0 font-mono text-xs text-amber-300">{formatCost(suggestion.price_current)}</span>
                          ) : null}
                        </div>
                        {suggestion.lr_category ? (
                          <div className="mt-0.5 text-xs text-stone-400">{formatCategoryLabel(suggestion.lr_category)}</div>
                        ) : null}
                      </div>
                    </button>
                  )
                })}
              </div>
            ) : null}
          </div>

          <div className="space-y-3">
            <label className="runic-heading block text-lg text-amber-100">Market Conditions</label>
            <select
              value={settlementType}
              onChange={(event) => setSettlementType(event.target.value)}
              className="stone-input w-full rounded-lg p-3 text-stone-100 outline-none"
            >
              {SETTLEMENT_OPTIONS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>

            <label
              className="flex cursor-pointer items-center gap-2 rounded-lg border border-amber-900/60 bg-zinc-950/50 px-3 py-2 text-sm text-stone-200"
              title="Apply +20% labor markup to priced items"
            >
              <input
                type="checkbox"
                checked={laborMarkupEnabled}
                onChange={(event) => setLaborMarkupEnabled(event.target.checked)}
                className="h-4 w-4 rounded border-amber-700 bg-zinc-900 text-amber-500 focus:ring-amber-500"
              />
              <span>Smith&apos;s Tithe (+20%)</span>
            </label>

            <button
              type="button"
              onClick={handleCalculate}
              disabled={loading}
              className="rune-button w-full rounded-lg px-4 py-3 font-semibold text-amber-100 transition disabled:cursor-not-allowed disabled:opacity-60"
            >
              {loading ? 'Consulting the runes...' : 'Appraise'}
            </button>

            {error ? <p className="rounded border border-red-700/60 bg-red-950/40 p-2 text-sm text-red-300">{error}</p> : null}
          </div>
        </section>

        {loading ? (
          <section className="mt-8 space-y-4 animate-pulse">
            <p className="text-sm text-amber-300">Consulting the runes...</p>
            <div className="h-8 w-48 rounded bg-zinc-800/80" />
            <div className="h-64 w-full rounded-lg border border-stone-700/70 bg-zinc-900/60" />
            <div className="h-24 w-full rounded-lg border border-amber-800/60 bg-zinc-900/60" />
          </section>
        ) : null}

        {result ? (
          <section className="mt-8 space-y-4">
            <h2 className="runic-heading text-2xl text-amber-100">Results</h2>
            <p className="rounded border border-emerald-700/50 bg-emerald-950/30 p-2 text-sm text-emerald-200">The runes hold true.</p>

            <div className="overflow-x-auto rounded-lg border border-stone-700/70">
              <table className="min-w-full divide-y divide-stone-700/70 text-left text-sm">
                <thead className="bg-zinc-900/80 text-stone-300">
                  <tr>
                    <th className="px-3 py-2">Item</th>
                    <th className="px-3 py-2">Quantity</th>
                    <th className="px-3 py-2">Empire Rate</th>
                    <th className="px-3 py-2">Crafting Cost</th>
                    <th className="px-3 py-2">Total Cost</th>
                    <th className="px-3 py-2">Source</th>
                    <th className="px-3 py-2">Details</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-stone-800/70 bg-zinc-950/60">
                  {displayedItems.map((entry) => {
                    const { index, item, unitCost, totalCost, qualityBreakdown } = entry
                    const sourceMeta = SOURCE_STYLES[item.source] || SOURCE_STYLES.unresolved
                    const hasQualityPrices = !!qualityBreakdown
                    const recipeDetail = item?.crafting_breakdown || (item.source === 'recipe' ? item : item?.recipe_alternative || null)
                    const hasRecipe = !!recipeDetail?.ingredients
                    const cleanedDisplayName = cleanDisplayName(item.display_name)
                    const completenessLabel = formatCompleteness(recipeDetail)
                    const isPartialRecipe = !!recipeDetail && !!recipeDetail.is_partial
                    const showLaborIndicator =
                      laborMarkupEnabled && item?.unit_cost !== null && item?.unit_cost !== undefined && !['unresolved', 'not_found'].includes(item?.source)

                    return (
                      <tr key={`${item.canonical_id || item.display_name || 'item'}-${index}`}>
                        <td className="px-3 py-3 text-stone-100">{cleanedDisplayName || 'Unknown item'}</td>
                        <td className="px-3 py-3 font-mono text-amber-200">{formatCost(item.quantity)}</td>
                        <td className="px-3 py-3 font-mono text-amber-200">
                          <div>{formatCost(item?.lr_unit_price)}</div>
                        </td>
                        <td className="px-3 py-3 font-mono text-amber-200">
                          <div>{formatCost(item?.crafting_cost)}</div>
                          {showLaborIndicator ? (
                            <div className="mt-1 text-xs font-normal text-amber-300">(+20% labor)</div>
                          ) : null}
                        </td>
                        <td className="px-3 py-3 font-mono text-amber-200">{formatCost(totalCost)}</td>
                        <td className="px-3 py-3">
                          <div className="flex flex-wrap items-center gap-2">
                            <span className={`inline-flex rounded-full px-2 py-1 text-xs font-semibold ${sourceMeta.classes}`}>
                              {sourceMeta.label}
                            </span>
                            {isPartialRecipe ? (
                              <span className="inline-flex rounded-full border border-rose-700/70 bg-rose-900/40 px-2 py-1 text-xs font-semibold text-rose-300">
                                Partial Cost
                              </span>
                            ) : null}
                          </div>
                        </td>
                        <td className="px-3 py-3">
                          <div className="space-y-2">
                            {isPartialRecipe && completenessLabel ? (
                              <div className="text-xs text-rose-300">{completenessLabel}</div>
                            ) : null}

                            {hasQualityPrices ? (
                              <details className="rounded border border-stone-700/70 bg-zinc-900/60 p-2">
                                <summary className="cursor-pointer text-xs font-semibold uppercase tracking-wide text-amber-300">
                                  Quality Breakdown
                                </summary>
                                <div className="mt-2 space-y-2">
                                  <div className="grid grid-cols-1 gap-2 text-xs sm:grid-cols-2 lg:grid-cols-3">
                                    {qualityBreakdown.tiers.map((tier) => {
                                      const tierPrice = getQualityValue(item.quality_prices, tier)
                                      const tierQty = Number(qualityBreakdown.allocation?.[tier] || 0)

                                      return (
                                        <div key={tier} className="rounded bg-zinc-950/60 p-2">
                                          <p className="uppercase text-stone-400">{tier}</p>
                                          <input
                                            type="number"
                                            min="0"
                                            step="1"
                                            value={tierQty}
                                            onChange={(event) =>
                                              handleQualityAllocationChange(index, tier, event.target.value)
                                            }
                                            className="mt-1 w-full rounded border border-amber-900/60 bg-zinc-900/80 px-2 py-1 font-mono text-amber-100 outline-none focus:border-amber-500"
                                          />
                                          <p className="mt-1 font-mono text-amber-200">@ {formatCost(tierPrice)}</p>
                                        </div>
                                      )
                                    })}
                                  </div>

                                  <p
                                    className={`text-xs ${
                                      qualityBreakdown.isAllocationValid ? 'text-emerald-300' : 'text-rose-300'
                                    }`}
                                  >
                                    {qualityBreakdown.isAllocationValid ? '✓' : '⚠'}{' '}
                                    {formatCost(qualityBreakdown.allocatedTotal)} of {formatCost(qualityBreakdown.quantity)} allocated
                                  </p>
                                </div>
                              </details>
                            ) : null}

                            {hasRecipe ? (
                              <details className="group rounded border border-stone-700/70 bg-zinc-900/60 p-2">
                                <summary className="cursor-pointer text-xs font-semibold uppercase tracking-wide text-amber-300">
                                  <span className="group-open:hidden">Reveal Runes</span>
                                  <span className="hidden group-open:inline">Conceal Runes</span>
                                </summary>
                                <div className="mt-2 text-xs">
                                  <div className="mb-2 grid grid-cols-[1fr_auto] gap-2 border-b border-stone-700/60 pb-1 text-[11px] uppercase tracking-wider text-stone-400">
                                    <span>Material</span>
                                    <span>Unit Price</span>
                                  </div>
                                  {renderIngredientTree(recipeDetail.ingredients)}
                                </div>
                              </details>
                            ) : null}

                            {!hasQualityPrices && !hasRecipe ? (
                              <span className="text-xs text-stone-500">No expanded details</span>
                            ) : null}
                          </div>
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>

            <div className="rounded-lg border border-amber-800/60 bg-zinc-900/60 p-4">
              <h3 className="font-serif text-xl text-amber-100">Order Totals</h3>
              <div className="mt-2 grid gap-2 text-sm sm:grid-cols-2">
                <p>
                  <span className="text-stone-400">Empire Price Total: </span>
                  <span className="font-mono text-emerald-300">{formatCost(displayedTotals.totalLRCost)}</span>
                </p>
                <p>
                  <span className="text-stone-400">Crafting Cost Total: </span>
                  <span className="font-mono text-amber-300">{formatCost(displayedTotals.totalRecipeCost)}</span>
                </p>
              </div>
              <p className="mt-3 text-lg">
                <span className="font-semibold text-stone-300">Combined Total: </span>
                <span className="font-mono text-2xl text-yellow-200">{formatCost(displayedTotals.totalCombined)}</span>
              </p>
              {displayedTotals.hasUnresolved ? (
                <p className="mt-3 rounded border border-red-700/60 bg-red-950/30 p-2 text-sm text-red-300">
                  ⚠ Some items are unresolved and may not be fully priced.
                </p>
              ) : null}
            </div>
          </section>
        ) : null}
      </div>

      {result ? (
        <aside
          className={`fixed right-0 top-1/2 z-20 -translate-y-1/2 transition-transform duration-300 ${
            diagOpen ? 'translate-x-0' : 'translate-x-[calc(100%-2.5rem)]'
          }`}
        >
          <div className="flex items-start">
            <button
              type="button"
              onClick={() => setDiagOpen((prev) => !prev)}
              className="mr-1 mt-5 rounded-l-lg border border-r-0 border-amber-700/70 bg-zinc-950/95 px-2 py-3 text-xs font-semibold uppercase tracking-wider text-amber-300 shadow-lg hover:bg-zinc-900"
              aria-label={diagOpen ? 'Collapse diagnostics panel' : 'Expand diagnostics panel'}
            >
              {diagOpen ? '⟩' : '⟨'}
            </button>

            <div className="h-[75vh] w-[22rem] overflow-y-auto rounded-l-xl border border-amber-800/70 bg-zinc-950/95 p-4 shadow-2xl shadow-black/70 backdrop-blur-sm">
              <h3 className="font-serif text-xl text-amber-100">Diagnostics</h3>

              <section className="mt-4 rounded-lg border border-stone-700/70 bg-zinc-900/60 p-3">
                <h4 className="font-semibold text-amber-200">Unresolved Items ({unresolvedItems.length})</h4>
                {unresolvedItems.length ? (
                  <ul className="mt-2 space-y-2 text-sm">
                    {unresolvedItems.map((item, index) => (
                      <li key={`${item.name}-${index}`} className="rounded border border-red-900/70 bg-red-950/30 p-2">
                        <p className="font-medium text-stone-100">
                          {item.name}
                          {item.count > 1 ? ` (×${item.count})` : ''}
                        </p>
                        <p className="text-xs text-red-300">{item.reason}</p>
                      </li>
                    ))}
                  </ul>
                ) : (
                  <p className="mt-2 text-sm text-stone-400">No unresolved items found in this calculation.</p>
                )}
              </section>

              <section className="mt-4 rounded-lg border border-stone-700/70 bg-zinc-900/60 p-3">
                <h4 className="font-semibold text-amber-200">Mod Coverage Gaps ({missingMods.length})</h4>
                {missingMods.length ? (
                  <ul className="mt-2 space-y-2 text-sm">
                    {missingMods.map((mod, index) => {
                      const cleanName = stripZipSuffix(mod.source_mod)
                      const pct = Number(mod.pct_missing)
                      const pctLabel = Number.isFinite(pct) ? Math.round(pct) : 0

                      return (
                        <li
                          key={`${mod.source_mod || 'mod'}-${index}`}
                          className="rounded border border-amber-900/60 bg-amber-950/20 p-2 text-stone-200"
                        >
                          <p className="font-medium text-amber-100">{cleanName || 'Unknown source mod'}</p>
                          <p className="text-xs text-amber-300">— {pctLabel}% unresolvable</p>
                        </li>
                      )
                    })}
                  </ul>
                ) : (
                  <p className="mt-2 text-sm text-stone-400">No significant recipe coverage gaps detected.</p>
                )}
              </section>
            </div>
          </div>
        </aside>
      ) : null}
    </div>
  )
}

export default App
