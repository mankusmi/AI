# Requirements Visualiser

`index.html` is a single-file page. Open it in a browser; there is nothing to install and it makes no network calls. Data is kept in the browser's local storage.

## Start with your own data

- **Blank register:** open **Import, export and reset** and click **Start blank** (click twice to confirm).
- **From a template:** paste the contents of `blank.json` into the same box and click **Load from box**. It holds one placeholder requirement showing every field.
- **From your tracker:** export a query from Azure DevOps or Jira as CSV and paste it under **DevOps integration**.

## Field values

- `priority`: `Must`, `Should`, `Could`, `Wont`
- `type`: `Business`, `Functional`, `Non-functional`, `Constraint`
- `status`: `Draft`, `Reviewed`, `Approved`, `Built`, `Verified`
- `env`: `Not started`, `Dev`, `Test`, `Staging`, `Production`
- `build`: `none`, `passing`, `failing`
- test `type`: `Unit`, `Integration`, `UAT`, `Performance`, `Accessibility`, `Security`
- test `status`: `Not run`, `Passed`, `Failed`, `Blocked`
- Only `title` is required. Test `ac` links a test to an acceptance criterion `id`.

Copy the JSON out of the import box now and then as a backup; clearing site data erases the register.
