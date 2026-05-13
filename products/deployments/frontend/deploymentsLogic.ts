import { actions, afterMount, kea, listeners, path, reducers, selectors } from 'kea'
import { actionToUrl, router, urlToAction } from 'kea-router'

import { objectsEqual } from 'lib/utils'
import { urls } from 'scenes/urls'

import type { deploymentsLogicType } from './deploymentsLogicType'
import {
    applyDeploymentFilters,
    cloneForRedeploy,
    cloneForRollback,
    DEFAULT_DEPLOYMENT_FILTERS,
    Deployment,
    DeploymentsFilters,
    DeploymentStatus,
} from './fixtures'

const FIXTURES_URL = '/static/deployments.fixtures.json'

const generateDeploymentId = (): string => `d-${Math.random().toString(36).slice(2, 9)}`

const filtersFromParams = (params: Record<string, any>): Partial<DeploymentsFilters> => {
    const out: Partial<DeploymentsFilters> = {}
    if (typeof params.search === 'string') {
        out.search = params.search
    }
    if (typeof params.author === 'string') {
        out.author = params.author
    }
    if (typeof params.order === 'string') {
        out.order = params.order
    }
    if (params.page !== undefined) {
        const page = parseInt(String(params.page))
        if (!isNaN(page)) {
            out.page = page
        }
    }
    if (typeof params.status === 'string') {
        out.status = params.status.split(',') as DeploymentStatus[]
    } else if (Array.isArray(params.status)) {
        out.status = params.status as DeploymentStatus[]
    }
    return out
}

const filtersToParams = (filters: DeploymentsFilters): Record<string, string | number> => {
    const params: Record<string, string | number> = {}
    if (filters.search) {
        params.search = filters.search
    }
    if (filters.author) {
        params.author = filters.author
    }
    if (filters.order !== DEFAULT_DEPLOYMENT_FILTERS.order) {
        params.order = filters.order
    }
    if (filters.page > 1) {
        params.page = filters.page
    }
    if (!objectsEqual([...filters.status].sort(), [...DEFAULT_DEPLOYMENT_FILTERS.status].sort())) {
        params.status = filters.status.join(',')
    }
    return params
}

export const deploymentsLogic = kea<deploymentsLogicType>([
    path(['products', 'deployments', 'frontend', 'deploymentsLogic']),
    actions({
        loadDeployments: true,
        setAllDeployments: (rows: Deployment[]) => ({ rows }),
        setFilters: (filters: Partial<DeploymentsFilters>) => ({ filters }),
        resetFilters: true,
        redeployDeployment: (id: string) => ({ id }),
        rollbackDeployment: (id: string) => ({ id }),
        _patchDeployment: (id: string, patch: Partial<Deployment>) => ({ id, patch }),
    }),
    reducers({
        allDeployments: [
            [] as Deployment[],
            {
                setAllDeployments: (_, { rows }) => rows,
                _patchDeployment: (state, { id, patch }) => state.map((d) => (d.id === id ? { ...d, ...patch } : d)),
            },
        ],
        allDeploymentsLoading: [
            false,
            {
                loadDeployments: () => true,
                setAllDeployments: () => false,
            },
        ],
        filters: [
            DEFAULT_DEPLOYMENT_FILTERS,
            {
                setFilters: (state, { filters }) => ({
                    ...state,
                    ...filters,
                    page:
                        filters.page ??
                        (filters.search !== undefined || filters.status || filters.author !== undefined
                            ? 1
                            : state.page),
                }),
                resetFilters: () => DEFAULT_DEPLOYMENT_FILTERS,
            },
        ],
    }),
    listeners(({ actions, values, cache }) => ({
        loadDeployments: async () => {
            const res = await fetch(FIXTURES_URL)
            const rows: Deployment[] = await res.json()
            actions.setAllDeployments(rows)
        },
        redeployDeployment: ({ id }) => {
            const original = values.allDeployments.find((d) => d.id === id)
            if (!original) {
                return
            }
            const newId = generateDeploymentId()
            const now = new Date().toISOString()
            const queued = cloneForRedeploy(original, newId, now)
            actions.setAllDeployments([queued, ...values.allDeployments])

            cache.disposables.add(() => {
                const t = window.setTimeout(() => {
                    actions._patchDeployment(newId, { status: 'initializing' })
                }, 1000)
                return () => clearTimeout(t)
            }, `redeploy-${newId}-init`)

            cache.disposables.add(() => {
                const t = window.setTimeout(() => {
                    actions._patchDeployment(newId, {
                        status: 'building',
                        started_at: new Date().toISOString(),
                    })
                }, 4000)
                return () => clearTimeout(t)
            }, `redeploy-${newId}-building`)

            cache.disposables.add(() => {
                const t = window.setTimeout(() => {
                    const succeeded = Math.random() > 0.1
                    const finishedAt = new Date().toISOString()
                    actions._patchDeployment(newId, {
                        status: succeeded ? 'ready' : 'error',
                        finished_at: finishedAt,
                        duration_seconds: 11,
                    })
                    if (succeeded) {
                        const prev = values.allDeployments.find((d) => d.is_current && d.id !== newId)
                        if (prev) {
                            actions._patchDeployment(prev.id, { is_current: false })
                        }
                        actions._patchDeployment(newId, { is_current: true })
                    }
                }, 15000)
                return () => clearTimeout(t)
            }, `redeploy-${newId}-finish`)
        },
        rollbackDeployment: ({ id }) => {
            const target = values.allDeployments.find((d) => d.id === id)
            if (!target || target.is_current) {
                return
            }
            const newId = generateDeploymentId()
            const now = new Date().toISOString()
            const rolledBack = cloneForRollback(target, newId, now)
            const withCurrentCleared = values.allDeployments.map((d) =>
                d.is_current ? { ...d, is_current: false } : d
            )
            actions.setAllDeployments([rolledBack, ...withCurrentCleared])
        },
    })),
    selectors({
        deployments: [
            (s) => [s.allDeployments, s.filters],
            (allDeployments, filters) => applyDeploymentFilters(allDeployments, filters),
        ],
        currentDeployment: [
            (s) => [s.allDeployments],
            (rows): Deployment | null => rows.find((d) => d.is_current) ?? null,
        ],
        authorOptions: [
            (s) => [s.allDeployments],
            (rows): { label: string; value: string }[] => {
                const seen = new Map<string, string>()
                rows.forEach((d) => {
                    if (d.commit_author_email && !seen.has(d.commit_author_email)) {
                        seen.set(d.commit_author_email, d.commit_author_name || d.commit_author_email)
                    }
                })
                return Array.from(seen.entries())
                    .map(([email, name]) => ({ label: name, value: email }))
                    .sort((a, b) => a.label.localeCompare(b.label))
            },
        ],
        hasActiveFilters: [
            (s) => [s.filters],
            (filters): boolean =>
                !!filters.search ||
                !!filters.author ||
                !objectsEqual([...filters.status].sort(), [...DEFAULT_DEPLOYMENT_FILTERS.status].sort()),
        ],
        shouldShowEmptyState: [
            (s) => [s.allDeployments, s.allDeploymentsLoading, s.hasActiveFilters],
            (rows, loading, hasFilters): boolean => !loading && rows.length === 0 && !hasFilters,
        ],
    }),
    actionToUrl(({ values }) => {
        const updateUrl = (): [string, Record<string, any>, Record<string, any>, { replace: boolean }] => {
            return [
                router.values.location.pathname,
                filtersToParams(values.filters),
                router.values.hashParams,
                { replace: true },
            ]
        }
        return {
            setFilters: updateUrl,
            resetFilters: updateUrl,
        }
    }),
    urlToAction(({ actions, values }) => ({
        [urls.deployments()]: (_, searchParams) => {
            const next = filtersFromParams(searchParams)
            const merged = { ...DEFAULT_DEPLOYMENT_FILTERS, ...next }
            if (!objectsEqual(merged, values.filters)) {
                actions.setFilters(merged)
            }
        },
    })),
    afterMount(({ actions }) => {
        actions.loadDeployments()
    }),
])
