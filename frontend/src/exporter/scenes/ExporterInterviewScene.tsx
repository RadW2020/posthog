import { useEffect, useRef, useState } from 'react'

import { LemonButton } from '@posthog/lemon-ui'

import { Logo } from 'lib/brand/Logo'

import { InterviewExportPayload } from '../types'

const VAPI_SDK_URL = 'https://cdn.jsdelivr.net/npm/@vapi-ai/web@latest/dist/index.umd.js'

declare global {
    interface Window {
        Vapi?: new (publicKey: string) => {
            start: (
                assistantId: string,
                overrides?: {
                    variableValues?: Record<string, string>
                    metadata?: Record<string, string>
                }
            ) => Promise<void>
            stop: () => void
            on: (event: string, cb: (...args: any[]) => void) => void
        }
    }
}

type CallState = 'idle' | 'loading' | 'connecting' | 'in-call' | 'ended' | 'error'

function loadVapiSdk(): Promise<void> {
    if (window.Vapi) {
        return Promise.resolve()
    }
    return new Promise((resolve, reject) => {
        const existing = document.querySelector<HTMLScriptElement>(`script[src="${VAPI_SDK_URL}"]`)
        if (existing) {
            existing.addEventListener('load', () => resolve())
            existing.addEventListener('error', () => reject(new Error('Failed to load Vapi SDK')))
            return
        }
        const script = document.createElement('script')
        script.src = VAPI_SDK_URL
        script.async = true
        script.onload = () => resolve()
        script.onerror = () => reject(new Error('Failed to load Vapi SDK'))
        document.head.appendChild(script)
    })
}

export default function ExporterInterviewScene({
    interview,
    accessToken,
    vapiPublicKey,
    vapiAssistantId,
}: {
    interview: InterviewExportPayload
    accessToken?: string
    vapiPublicKey?: string
    vapiAssistantId?: string
}): JSX.Element {
    const [state, setState] = useState<CallState>('idle')
    const [errorMessage, setErrorMessage] = useState<string | null>(null)
    const vapiRef = useRef<InstanceType<NonNullable<Window['Vapi']>> | null>(null)

    useEffect(() => {
        document.title = `Interview · ${interview.topic}`
    }, [interview.topic])

    useEffect(() => {
        return () => {
            vapiRef.current?.stop()
        }
    }, [])

    const start = async (): Promise<void> => {
        if (!vapiPublicKey || !vapiAssistantId) {
            setErrorMessage(
                'This interview is not fully configured — the host needs to set the Vapi public key and assistant id.'
            )
            setState('error')
            return
        }
        setState('loading')
        try {
            await loadVapiSdk()
            const VapiCtor = window.Vapi
            if (!VapiCtor) {
                throw new Error('Vapi SDK did not load')
            }
            const vapi = new VapiCtor(vapiPublicKey)
            vapiRef.current = vapi
            vapi.on('call-end', () => setState('ended'))
            vapi.on('error', (e: unknown) => {
                setErrorMessage(e instanceof Error ? e.message : 'Vapi reported an error during the call.')
                setState('error')
            })
            setState('connecting')
            await vapi.start(vapiAssistantId, {
                variableValues: {
                    userName: interview.user_name,
                    topic: interview.topic,
                    agent_context: interview.agent_context,
                    questions: JSON.stringify(interview.questions || []),
                },
                metadata: {
                    topic_id: interview.topic_id,
                    interviewee_identifier: interview.interviewee_identifier,
                    sharing_access_token: accessToken ?? '',
                },
            })
            setState('in-call')
        } catch (e) {
            setErrorMessage(e instanceof Error ? e.message : 'Failed to start interview.')
            setState('error')
        }
    }

    const stop = (): void => {
        vapiRef.current?.stop()
        setState('ended')
    }

    return (
        <div className="max-w-2xl mx-auto px-4 py-12">
            <div className="mb-8 flex items-center justify-between">
                <Logo className="text-lg" />
                <span className="text-xs text-muted">Powered by PostHog</span>
            </div>

            <h1 className="text-3xl font-bold mb-4">Hi {interview.user_name}!</h1>

            <p className="text-lg mb-4">
                We're researching <strong>{interview.topic}</strong> and would love to hear your perspective.
            </p>

            <p className="text-muted mb-6">
                This is a 5–10 minute voice conversation with an AI interviewer. Talk like you would to a researcher on
                our team — your feedback helps us build a better product.
            </p>

            <div className="bg-accent-highlight border border-accent p-4 rounded mb-8 text-sm">
                <strong>How it works</strong>
                <ol className="list-decimal pl-5 mt-2 space-y-1">
                    <li>
                        Click <em>Start interview</em> below.
                    </li>
                    <li>Allow microphone access when prompted.</li>
                    <li>Have a casual conversation — the AI will guide you through a few questions.</li>
                    <li>You can end the call any time.</li>
                </ol>
            </div>

            {state === 'idle' && (
                <LemonButton type="primary" size="large" fullWidth onClick={start}>
                    Start interview
                </LemonButton>
            )}
            {state === 'loading' && <p>Loading interviewer…</p>}
            {state === 'connecting' && <p>Connecting…</p>}
            {state === 'in-call' && (
                <div>
                    <p className="mb-4">You're live with the interviewer.</p>
                    <LemonButton type="secondary" onClick={stop}>
                        End interview
                    </LemonButton>
                </div>
            )}
            {state === 'ended' && (
                <p className="text-success">
                    Thanks for taking the time! Your conversation has been recorded — we'll be in touch.
                </p>
            )}
            {state === 'error' && (
                <div className="text-danger">
                    <p className="mb-2">{errorMessage ?? 'Something went wrong.'}</p>
                    <LemonButton type="secondary" onClick={() => setState('idle')}>
                        Try again
                    </LemonButton>
                </div>
            )}

            <p className="text-xs text-muted text-center mt-12">
                Your conversation will be transcribed and analyzed to help improve PostHog. We won't share your
                individual responses publicly.
            </p>
        </div>
    )
}
