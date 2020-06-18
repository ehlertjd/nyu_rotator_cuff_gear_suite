# NYU/Siemens Rotator Cuff Tear Assessment Gear Suite

A suite of custom gears that assign readers, assign cases and gather assessed case data to provide a summary report for the management for the NYU/Siemens Rotator Cuff Tear Project.

## Objectives

In order to achieve the desired workflow, four separate gears are developed:

1. [**Assign-Readers**](./gears/assign_readers/)

    This gear creates, initializes, and modifies projects with permission for one reader each.  This is done either in bulk with a csv file or with an individual specified in the gear configuration.  All reader projects are created within the “Readers” group.

2. [**Assign-Cases**](./gears/assign_cases/)

    This gear distributes each case to three randomly selected distinct reader projects for assessment. Readers are selected without replacement.

3. [**Gather-Cases**](./gears/gather_cases/)

    This gear gathers case assessment status and assessment data into the session of origin.

4. [**Assign-Single-Case**](./gears/assign_single_case/)

    This gear is used for the assignment or reassignment of a specific single case to a single reader.

The gears above keep track of case assignments and assignment status in the metadata associated with each respective Flywheel container.  These metadata are backed up within csv files that are the outputs of both the assign-cases and the gather-cases gears.  Should the metadata in the projects accidentally get corrupted, these csv files can be used to restore the state of the system.

**NOTE:** All gears must be run from within a "Master Project". Attempting to execute any gear from within a reader project will fail.
