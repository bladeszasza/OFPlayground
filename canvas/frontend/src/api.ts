let authorization: Promise<void> | null = null

async function authorize() {
  authorization ??= fetch('/auth/bootstrap', { credentials: 'same-origin' }).then(response => {
    if (!response.ok) throw new Error('Open Canvas through its local server to authorize this browser.')
  }).catch(error => { authorization = null; throw error })
  await authorization
}

export async function apiFetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  await authorize()
  let response = await fetch(input, { ...init, credentials: 'same-origin' })
  if (response.status === 401) {
    authorization = null
    await authorize()
    response = await fetch(input, { ...init, credentials: 'same-origin' })
  }
  return response
}
