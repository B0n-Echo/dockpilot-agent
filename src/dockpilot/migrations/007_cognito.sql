-- Real sign-in with Cognito (dp-24): Cognito's sign-in session for a code it emailed. Working copy only, additive.
-- In Cognito mode code_hash holds the marker 'cognito': the code itself is made and checked by Cognito, never by us.
ALTER TABLE login_codes ADD COLUMN cognito_session TEXT;
